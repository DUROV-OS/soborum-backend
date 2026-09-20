"""Автоматическое сопоставление материала шаблона графа этапов (0066-d) с
карточкой материала на складе (0073-a).

`WarehouseMaterial` (app.warehouse.models) не хранит ничего, кроме
title+code — один и тот же реальный материал может иметь несколько карточек
(разные поставщики, чуть другой размер), поэтому сопоставление по строке не
надёжно. Как и `app.warehouse.price_import` (ИИ-разметка колонок прайса
поставщика): ИИ решает сам, когда уверен, и явно отказывается, когда нет —
"на всякий случай" карточку не выбирает никто. Единственная точка сетевого
вызова вынесена в `_call_ai`, тесты монки-патчат именно её (тот же приём,
что и `stage_template_service._call_ai` / `app.ai.priorities`).

Успешные сопоставления (`high`/`medium`) кэшируются в
`TemplateMaterialMapping` по (normalized_name, unit) и переиспользуются без
повторного обращения к ИИ при следующей генерации шаблона для похожего
материала. `no_match` НЕ кэшируется — по мере роста каталога склада система
продолжает переспрашивать ИИ. Человеческая правка (`record_human_match`,
вызывается из `stage_template_service.update_material`) пишется в тот же
кэш с `matched_by=HUMAN` и с этого момента побеждает над мнением ИИ по той
же паре — человека повторно не переспрашивают.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.config import settings
from app.production.models import MappingConfidence, MatchedBy, TemplateMaterialMapping
from app.warehouse.models import WarehouseMaterial

# Тот же приём, что MAX_CANDIDATES в app/ai/priorities.py — не заваливаем
# промпт всем каталогом склада разом.
MAX_CANDIDATES = 40

SUBMIT_TOOL_NAME = "submit_warehouse_match"

SYSTEM_PROMPT = (
    "Ты сопоставляешь материал из графа этапов производства модульных домов "
    "«Soborbum» с карточками материалов на складе. Тебе передано название и "
    "единица измерения материала из шаблона, а также список карточек-"
    "кандидатов со склада (id, название, код, единица измерения, категория).\n\n"
    "Правила:\n"
    "1. Если это точно тот же материал (совпадение по сути, а не только по "
    "словам) — верни warehouse_material_id и confidence=\"high\".\n"
    "2. Если карточка подходит по смыслу, но есть неоднозначность (похожий "
    "материал от другого поставщика, чуть другой размер/исполнение, а "
    "текст материала это не уточняет) — верни warehouse_material_id и "
    "confidence=\"medium\".\n"
    "3. Если ни одна карточка не подходит уверенно — верни no_match=true. "
    "Никогда не выбирай карточку «на всякий случай», если сомневаешься, что "
    "это тот же материал.\n"
    "Отвечай ТОЛЬКО вызовом инструмента submit_warehouse_match, без текста."
)

TOOL_SCHEMA = {
    "name": SUBMIT_TOOL_NAME,
    "description": "Сопоставить материал шаблона графа этапов с карточкой материала на складе.",
    "input_schema": {
        "type": "object",
        "properties": {
            "warehouse_material_id": {
                "type": "integer",
                "description": "id карточки склада-кандидата, если найдено уверенное соответствие.",
            },
            "confidence": {
                "type": "string",
                "enum": ["high", "medium"],
                "description": "Обязательно, если указан warehouse_material_id.",
            },
            "no_match": {
                "type": "boolean",
                "description": "true, если ни одна карточка не подходит уверенно — тогда остальные поля не нужны.",
            },
        },
        "required": [],
    },
}


@dataclass
class MatchResult:
    warehouse_material_id: int | None
    confidence: str | None  # "high" | "medium" | None


def _normalize(name: str) -> str:
    return " ".join((name or "").strip().lower().split())


def _lookup_cached(db: Session, normalized_name: str, unit: str) -> TemplateMaterialMapping | None:
    return (
        db.query(TemplateMaterialMapping)
        .filter(
            TemplateMaterialMapping.normalized_name == normalized_name,
            TemplateMaterialMapping.unit == unit,
        )
        .first()
    )


def _candidate_materials(db: Session, name: str) -> list[WarehouseMaterial]:
    """Сужаем каталог по пересечению слов названия (сначала самые
    информативные — «свая», «брус» — потом размер/исполнение), чтобы на
    большом каталоге лимит кандидатов не отрезал настоящее совпадение.
    Если пересечений нет вообще — отдаём срез каталога как есть (тот же
    компромисс, что MAX_CANDIDATES в app/ai/priorities.py)."""
    all_materials = db.query(WarehouseMaterial).order_by(WarehouseMaterial.id).all()
    words = [w for w in _normalize(name).split() if len(w) >= 3]
    if not words:
        return all_materials[:MAX_CANDIDATES]

    scored = [(sum(1 for w in words if w in _normalize(m.title)), m) for m in all_materials]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    top = [m for score, m in scored if score > 0][:MAX_CANDIDATES]
    return top or all_materials[:MAX_CANDIDATES]


def _call_ai(name: str, unit: str, candidates: list[WarehouseMaterial]) -> dict:
    from app.core.llm import anthropic_client

    payload = [
        {"id": c.id, "title": c.title, "code": c.code, "unit": c.unit, "category": c.category.value}
        for c in candidates
    ]
    user = (
        f"Материал шаблона: «{name}», единица измерения: «{unit}».\n\n"
        f"Кандидаты со склада ({len(payload)}):\n" + json.dumps(payload, ensure_ascii=False)
    )
    client = anthropic_client(timeout=45.0, max_retries=2)
    response = client.messages.create(
        model=settings.ai_model,
        max_tokens=256,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user}],
        tools=[TOOL_SCHEMA],
        tool_choice={"type": "tool", "name": SUBMIT_TOOL_NAME},
    )
    block = next((b for b in response.content if b.type == "tool_use"), None)
    if block is None:
        raise RuntimeError("no tool_use in submit_warehouse_match response")
    return block.input or {}


def _save_mapping(
    db: Session,
    normalized_name: str,
    unit: str,
    warehouse_material_id: int,
    confidence: MappingConfidence,
    matched_by: MatchedBy,
) -> None:
    existing = _lookup_cached(db, normalized_name, unit)
    if existing is not None:
        existing.warehouse_material_id = warehouse_material_id
        existing.confidence = confidence
        existing.matched_by = matched_by
    else:
        db.add(
            TemplateMaterialMapping(
                normalized_name=normalized_name,
                unit=unit,
                warehouse_material_id=warehouse_material_id,
                confidence=confidence,
                matched_by=matched_by,
            )
        )
    db.flush()


def match_template_material(db: Session, name: str, unit: str) -> MatchResult:
    """Best-effort: любая ошибка ИИ (нет ключа, таймаут, сбой) — это
    `MatchResult(None, None)`, не исключение. Вызывающая сторона
    (`stage_template_service._persist_draft`) не должна падать или блокировать
    генерацию шаблона из-за сопоставления со складом."""
    normalized = _normalize(name)
    cached = _lookup_cached(db, normalized, unit)
    if cached is not None:
        return MatchResult(cached.warehouse_material_id, cached.confidence.value)

    if not settings.anthropic_api_key:
        return MatchResult(None, None)

    candidates = _candidate_materials(db, name)
    if not candidates:
        return MatchResult(None, None)

    try:
        raw = _call_ai(name, unit, candidates)
    except Exception:  # noqa: BLE001 — сопоставление best-effort, сеть/ИИ не должны ронять генерацию
        return MatchResult(None, None)

    if raw.get("no_match"):
        return MatchResult(None, None)

    warehouse_material_id = raw.get("warehouse_material_id")
    confidence = raw.get("confidence")
    candidate_ids = {c.id for c in candidates}
    if warehouse_material_id not in candidate_ids or confidence not in ("high", "medium"):
        # Модель не выполнила контракт (не тот id / нет confidence) — это не
        # то же самое, что осознанный no_match, но выдумывать соответствие
        # нельзя, поэтому падаем на тот же безопасный результат.
        return MatchResult(None, None)

    _save_mapping(db, normalized, unit, warehouse_material_id, MappingConfidence(confidence), MatchedBy.AI)
    return MatchResult(warehouse_material_id, confidence)


def record_human_match(db: Session, name: str, unit: str, warehouse_material_id: int) -> None:
    """Человек подтвердил/поправил сопоставление на проверке шаблона
    ([[0066-e]], `stage_template_service.update_material`) — запоминаем как
    HIGH-уверенность с matched_by=HUMAN: следующая генерация того же
    материала по смыслу больше не спрашивает ИИ и не предлагает более раннее
    ИИ-сопоставление той же пары."""
    normalized = _normalize(name)
    _save_mapping(db, normalized, unit, warehouse_material_id, MappingConfidence.HIGH, MatchedBy.HUMAN)


def backfill_unmatched(db: Session) -> dict:
    """Разовый прогон по существующим `TemplateBlockMaterial` без
    `warehouse_material_id` (задачи, заведённые в производстве до 0073-a
    руками не считаются — трогаем только сами материалы шаблона). Запускать
    вручную: `python -m app.production.material_matching`."""
    from app.production.stage_templates import TemplateBlockMaterial

    materials = (
        db.query(TemplateBlockMaterial).filter(TemplateBlockMaterial.warehouse_material_id.is_(None)).all()
    )
    matched = 0
    for material in materials:
        result = match_template_material(db, material.name, material.unit)
        if result.warehouse_material_id is not None:
            material.warehouse_material_id = result.warehouse_material_id
            material.confidence = MappingConfidence(result.confidence)
            matched += 1
    db.flush()
    return {"total": len(materials), "matched": matched}


if __name__ == "__main__":
    from app.db.session import SessionLocal

    session = SessionLocal()
    try:
        stats = backfill_unmatched(session)
        session.commit()
        print(f"Материалов без склада: {stats['total']}, сопоставлено: {stats['matched']}")
    finally:
        session.close()
