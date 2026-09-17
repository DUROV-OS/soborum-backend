"""Виджет «Сроки» на «Главной» одного производства (0065-b): главная
задержка/ожидание/бутылочное горлышко, которое сейчас влияет на срок именно
этого дома, с объяснением, как именно оно влияет.

Собирает кандидатов из тех же фактов, что и «Требует внимания» (0065-a) —
просроченные задачи блоков, зависшие заявки на материалы, ещё не поданные
заявки при недостаче — и просит Claude выбрать САМОЕ значимое узкое место
(по образцу `dashboard/aktualnoe.ai_rate_cycles`). Без `ANTHROPIC_API_KEY`
или при сбое сети — детерминированный fallback по приоритету типа сигнала,
без ИИ-текста, но и без выдумывания фактов сверх того, что реально в БД.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.ai import cache as ai_cache
from app.core.config import settings
from app.production.models import (
    BlockMaterial,
    MaterialRequest,
    MaterialRequestStatus,
    Production,
    ProductionBlock,
)
from app.production.schemas import DeadlineInsightOut
from app.tasks.models import Task, TaskStatus

CACHE_TTL = timedelta(hours=6)

# Приоритет типов сигналов при выборе fallback-узкого места (первый найденный
# тип побеждает; внутри типа — самый старый по `since`).
SIGNAL_PRIORITY = ("overdue_task", "pending_material_request", "material_shortfall")


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


@dataclass
class DeadlineSignal:
    kind: str
    block_name: str
    detail: str
    since: datetime | None

    @property
    def days_since(self) -> int | None:
        if self.since is None:
            return None
        return max(0, (datetime.now(timezone.utc) - self.since).days)


def collect_deadline_signals(db: Session, production: Production) -> list[DeadlineSignal]:
    blocks = (
        db.query(ProductionBlock).filter(ProductionBlock.production_id == production.id).all()
    )
    block_ids = [b.id for b in blocks]
    block_name_by_id = {b.id: b.name for b in blocks}
    if not block_ids:
        return []

    signals: list[DeadlineSignal] = []
    now = datetime.now(timezone.utc)

    overdue_tasks = (
        db.query(Task)
        .filter(Task.block_id.in_(block_ids), Task.status != TaskStatus.DONE, Task.deadline.isnot(None))
        .all()
    )
    for task in overdue_tasks:
        deadline = _aware(task.deadline)
        if deadline < now:
            signals.append(
                DeadlineSignal(
                    kind="overdue_task",
                    block_name=block_name_by_id.get(task.block_id, "?"),
                    detail=task.title,
                    since=deadline,
                )
            )

    pending_requests = (
        db.query(MaterialRequest)
        .join(BlockMaterial, MaterialRequest.block_material_id == BlockMaterial.id)
        .filter(BlockMaterial.block_id.in_(block_ids), MaterialRequest.status == MaterialRequestStatus.PENDING)
        .all()
    )
    for request in pending_requests:
        block_material = request.block_material
        signals.append(
            DeadlineSignal(
                kind="pending_material_request",
                block_name=block_name_by_id.get(block_material.block_id, "?"),
                detail=f"{request.warehouse_material.title} × {request.quantity} {block_material.unit}",
                since=_aware(request.created_at),
            )
        )

    requested_material_ids = {r.block_material_id for r in pending_requests}
    shortfalls = (
        db.query(BlockMaterial)
        .filter(
            BlockMaterial.block_id.in_(block_ids),
            BlockMaterial.quantity_required > 0,
            BlockMaterial.quantity_requested == 0,
        )
        .all()
    )
    for material in shortfalls:
        if material.id in requested_material_ids:
            continue
        signals.append(
            DeadlineSignal(
                kind="material_shortfall",
                block_name=block_name_by_id.get(material.block_id, "?"),
                detail=f"{material.warehouse_material.title} — не хватает {material.quantity_required} {material.unit}",
                since=None,
            )
        )

    return signals


def _fallback_insight(signals: list[DeadlineSignal]) -> DeadlineInsightOut:
    by_kind: dict[str, list[DeadlineSignal]] = {}
    for signal in signals:
        by_kind.setdefault(signal.kind, []).append(signal)

    kind = next((k for k in SIGNAL_PRIORITY if k in by_kind), None)
    candidates = by_kind[kind]
    # Самый старый (без даты — в конец) в пределах выбранного типа.
    chosen = min(candidates, key=lambda s: s.since or datetime.now(timezone.utc))

    if kind == "overdue_task":
        days = chosen.days_since or 0
        return DeadlineInsightOut(
            title=f"Просрочена задача блока «{chosen.block_name}»",
            description=f"«{chosen.detail}» просрочена на {days} дн.",
            impact="Пока задача не закрыта, следующий этап этого блока не может начаться вовремя.",
            source="fallback",
        )
    if kind == "pending_material_request":
        days = chosen.days_since or 0
        return DeadlineInsightOut(
            title="Заявка на материал ждёт решения склада",
            description=f"Блок «{chosen.block_name}»: заявка на {chosen.detail} подана {days} дн. назад, склад ещё не ответил.",
            impact="Без материала блок не может продолжить сборку — это и есть текущее узкое место.",
            source="fallback",
        )
    return DeadlineInsightOut(
        title="Материал ещё не запрошен со склада",
        description=f"Блок «{chosen.block_name}»: {chosen.detail}, заявка на склад пока не подана.",
        impact="Пока заявку не подали, материал не начнёт путь со склада — это может задержать этот блок.",
        source="fallback",
    )


SUBMIT_TOOL_NAME = "submit_deadline_insight"

SYSTEM_PROMPT = (
    "Ты — аналитик системы управления производством модульных домов «Soborbum». "
    "Тебе передан список сигналов (потенциальных узких мест) по ОДНОМУ дому в "
    "производстве: просроченные задачи блоков, заявки на материалы, ожидающие "
    "решения склада, и материалы, которые ещё не запрошены при недостаче.\n\n"
    "Выбери РОВНО ОДИН, самый значимый для срока сигнал (приоритет: просроченная "
    "задача > зависшая заявка на материал > ещё не запрошенный материал; при "
    "прочих равных — тот, что длится дольше). Заполни:\n"
    "- title — короткий (3-6 слов) заголовок узкого места.\n"
    "- description — что именно происходит, СТРОГО по переданным данным, не "
    "выдумывай факты и цифры, которых нет во входе.\n"
    "- impact — как именно это влияет на срок дома (1-2 предложения).\n"
    "Отвечай ТОЛЬКО вызовом инструмента submit_deadline_insight, без текста."
)

TOOL_SCHEMA = {
    "name": SUBMIT_TOOL_NAME,
    "description": "Отправить главное узкое место, влияющее на срок дома.",
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "description": {"type": "string"},
            "impact": {"type": "string"},
        },
        "required": ["title", "description", "impact"],
    },
}


def _ai_pick_bottleneck(signals: list[DeadlineSignal]) -> dict | None:
    if not settings.anthropic_api_key or not signals:
        return None

    from app.core.llm import anthropic_client

    payload = [
        {
            "kind": s.kind,
            "block": s.block_name,
            "detail": s.detail,
            "days_since": s.days_since,
        }
        for s in signals
    ]

    try:
        response = anthropic_client().messages.create(
            model=settings.ai_model,
            max_tokens=512,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            tools=[TOOL_SCHEMA],
            tool_choice={"type": "tool", "name": SUBMIT_TOOL_NAME},
        )
    except Exception:  # noqa: BLE001 — сеть/квоты/парсинг: молча деградируем
        return None

    tool_use = next((b for b in response.content if b.type == "tool_use"), None)
    if tool_use is None:
        return None

    title = str(tool_use.input.get("title") or "").strip()
    description = str(tool_use.input.get("description") or "").strip()
    impact = str(tool_use.input.get("impact") or "").strip()
    if not (title and description and impact):
        return None
    return {"title": title, "description": description, "impact": impact}


def _build(db: Session, production: Production) -> DeadlineInsightOut:
    signals = collect_deadline_signals(db, production)
    if not signals:
        return DeadlineInsightOut(
            title="По графику",
            description="Узких мест, влияющих на срок этого дома, не найдено.",
            impact="",
            source="none",
        )

    ai_result = _ai_pick_bottleneck(signals)
    if ai_result is not None:
        return DeadlineInsightOut(**ai_result, source="ai")
    return _fallback_insight(signals)


def generate_deadline_insight(db: Session, production: Production, force: bool = False) -> DeadlineInsightOut:
    cache_key = f"production_home_deadlines:{production.id}"
    cached = ai_cache.get(db, cache_key, force, ttl=CACHE_TTL)
    if cached is not None:
        return DeadlineInsightOut(**cached)

    result = _build(db, production)
    # Не кешируем "не найдено"/fallback — как только появятся сигналы или ключ,
    # следующий запрос должен пересчитать, а не ждать 6 часов.
    if result.source == "ai":
        ai_cache.set(db, cache_key, result.model_dump(mode="json"), datetime.now(timezone.utc))
    return result
