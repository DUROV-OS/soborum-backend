"""ИИ-генерация шаблона графа этапов производства по постраничному разбору КР
(0066-d) + правка и подтверждение инженером/начальником производства.

Как и `production/deadlines.py::_ai_pick_bottleneck` — единственный сетевой
вызов к Claude вынесен в отдельную функцию (`_call_ai`) с принудительным
`tool_choice`, системный промпт явно запрещает выдумывать этапы/задачи/
материалы сверх переданного текста страниц и требует ссылку на страницу КР
у каждой составляющей. Тесты монки-патчат `_call_ai`, а не сеть.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.clients.models import Client
from app.core.config import settings
from app.production import kr_extraction, material_matching
from app.production.models import MappingConfidence
from app.production.stage_templates import (
    ProductionStageTemplate,
    TemplateBlock,
    TemplateBlockMaterial,
    TemplateBlockTask,
    TemplateStatus,
)

_MAX_PAGE_CHARS = 2000

SUBMIT_TOOL_NAME = "submit_stage_template"

SYSTEM_PROMPT = (
    "Ты — инженер-технолог производства модульных домов «Soborbum». Тебе передан "
    "постраничный текст КР (конструктивных решений) одного дома — JSON-список "
    "{page_number, text}.\n\n"
    "Разбей производство этого дома на последовательные и (где это верно по КР) "
    "параллельные блоки-этапы. Для каждого блока укажи задачи и материалы. "
    "СТРОГО следуй правилам:\n"
    "1. Не выдумывай ничего, чего нет в переданном тексте — ни этапов, ни "
    "материалов, ни количеств. Если факта в тексте нет — не включай его.\n"
    "2. У КАЖДОГО блока, задачи и материала обязательна ссылка на номер "
    "страницы (kr_page_ref / kr_page_refs), с которой это взято.\n"
    "3. Зависимости блока (depends_on_sequence) указывай через sequence "
    "других блоков ЭТОГО ЖЕ ответа, только если КР явно подразумевает порядок "
    "(нельзя начать одно, не закончив другое).\n"
    "Отвечай ТОЛЬКО вызовом инструмента submit_stage_template, без текста."
)

TOOL_SCHEMA = {
    "name": SUBMIT_TOOL_NAME,
    "description": "Отправить предложенный граф этапов производства по КР.",
    "input_schema": {
        "type": "object",
        "properties": {
            "blocks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "sequence": {"type": "integer"},
                        "depends_on_sequence": {"type": "array", "items": {"type": "integer"}},
                        "kr_page_refs": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "page_number": {"type": "integer"},
                                    "note": {"type": "string"},
                                },
                                "required": ["page_number"],
                            },
                        },
                        "tasks": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "title": {"type": "string"},
                                    "description": {"type": "string"},
                                    "kr_page_ref": {
                                        "type": "object",
                                        "properties": {
                                            "page_number": {"type": "integer"},
                                            "note": {"type": "string"},
                                        },
                                        "required": ["page_number"],
                                    },
                                },
                                "required": ["title", "kr_page_ref"],
                            },
                        },
                        "materials": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "unit": {"type": "string"},
                                    "kr_page_ref": {
                                        "type": "object",
                                        "properties": {
                                            "page_number": {"type": "integer"},
                                            "note": {"type": "string"},
                                        },
                                        "required": ["page_number"],
                                    },
                                },
                                "required": ["name", "unit", "kr_page_ref"],
                            },
                        },
                    },
                    "required": ["name", "sequence", "kr_page_refs", "tasks", "materials"],
                },
            },
        },
        "required": ["blocks"],
    },
}


def _find_existing_template(db: Session, house_model_key: str | None) -> ProductionStageTemplate | None:
    """Шаблон уже есть для этой модели дома (в любом статусе) — переиспользуем
    без обращения к ИИ. Индивидуальные проекты (`house_model_key is None`) не
    переиспользуются — каждый раз строятся заново и остаются одноразовыми."""
    if not house_model_key:
        return None
    return (
        db.query(ProductionStageTemplate)
        .filter(ProductionStageTemplate.house_model_key == house_model_key)
        .order_by(ProductionStageTemplate.id.desc())
        .first()
    )


def _kr_payload(extraction) -> list[dict]:
    payload = []
    for page in extraction.pages:
        text = (page.get("text") or "").strip()
        if not text:
            continue
        payload.append({"page_number": page["page_number"], "text": text[:_MAX_PAGE_CHARS]})
    return payload


def _call_ai(pages_payload: list[dict]) -> dict:
    """Единственная точка сетевого вызова Claude — вынесена отдельно, чтобы
    тесты монки-патчили именно её (как `deadlines._ai_pick_bottleneck`),
    не поднимая реальную сеть, и считали число вызовов."""
    from app.core.llm import anthropic_client

    # Полный граф на реальный многостраничный КР — не короткая структурированная
    # реплика вроде deadlines._ai_pick_bottleneck: генерация с max_tokens=16000
    # не укладывается в дефолтный клиентский timeout (60с, рассчитан на быстрые
    # вызовы) — раньше запрос обрывался клиентом раньше, чем модель успевала
    # дописать JSON. Увеличены оба параметра.
    response = anthropic_client(timeout=300.0).messages.create(
        model=settings.ai_model,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": json.dumps(pages_payload, ensure_ascii=False)}],
        tools=[TOOL_SCHEMA],
        tool_choice={"type": "tool", "name": SUBMIT_TOOL_NAME},
    )
    tool_use = next((b for b in response.content if b.type == "tool_use"), None)
    if tool_use is None:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="ИИ не вернул структурированный ответ")
    if response.stop_reason == "max_tokens":
        # Ответ оборван на середине JSON — нельзя тихо принять как «ИИ
        # предложил пустой/неполный граф», это не то же самое, что реальное
        # решение модели. Явная ошибка вместо недостоверного результата.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Ответ ИИ оборван по лимиту токенов — граф не сформирован полностью, попробуйте ещё раз",
        )
    return _normalize_graph(tool_use.input)


def _normalize_graph(raw: dict) -> dict:
    """На реальном большом графе модель иногда отдаёт `blocks` не массивом, а
    строкой с сериализованным JSON (в т.ч. второй раз обёрнутым в
    `{"blocks": [...]}`) — сама структура при этом валидна и не выдумана,
    просто не в той форме, которую ожидает схема инструмента. Разворачиваем
    оба варианта; если после этого `blocks` всё равно не список — это уже
    настоящая ошибка формата, а не то, что можно тихо принять."""
    blocks = raw.get("blocks")
    if isinstance(blocks, str):
        try:
            parsed = json.loads(blocks)
        except json.JSONDecodeError:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="ИИ вернул нераспознаваемую структуру графа")
        blocks = parsed.get("blocks") if isinstance(parsed, dict) else parsed
    if not isinstance(blocks, list) or not blocks:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="ИИ не предложил ни одного блока по этому КР")
    return {"blocks": blocks}


def _persist_draft(db: Session, client: Client, graph: dict) -> ProductionStageTemplate:
    template = ProductionStageTemplate(
        house_model_key=client.house_model_key,
        status=TemplateStatus.DRAFT,
        source_client_id=client.id,
    )
    db.add(template)
    db.flush()

    blocks_by_sequence: dict[int, TemplateBlock] = {}
    for raw_block in graph.get("blocks", []):
        block = TemplateBlock(
            template_id=template.id,
            name=raw_block["name"],
            description=raw_block.get("description"),
            sequence=raw_block["sequence"],
            kr_page_refs=raw_block.get("kr_page_refs", []),
        )
        db.add(block)
        db.flush()
        blocks_by_sequence[raw_block["sequence"]] = block

        for raw_task in raw_block.get("tasks", []):
            db.add(
                TemplateBlockTask(
                    template_block_id=block.id,
                    title=raw_task["title"],
                    description=raw_task.get("description"),
                    kr_page_ref=raw_task.get("kr_page_ref"),
                )
            )
        for raw_material in raw_block.get("materials", []):
            # Сопоставление со складом (0073-a) — best-effort, "сразу" в этом
            # же проходе: любой сбой ИИ оставляет warehouse_material_id
            # пустым, генерация шаблона не падает и не блокируется
            # (см. material_matching.match_template_material).
            match = material_matching.match_template_material(db, raw_material["name"], raw_material["unit"])
            db.add(
                TemplateBlockMaterial(
                    template_block_id=block.id,
                    name=raw_material["name"],
                    unit=raw_material["unit"],
                    kr_page_ref=raw_material.get("kr_page_ref"),
                    warehouse_material_id=match.warehouse_material_id,
                    confidence=MappingConfidence(match.confidence) if match.confidence else None,
                )
            )
    db.flush()

    for raw_block in graph.get("blocks", []):
        block = blocks_by_sequence.get(raw_block["sequence"])
        if block is None:
            continue
        for dep_sequence in raw_block.get("depends_on_sequence", []):
            dep_block = blocks_by_sequence.get(dep_sequence)
            if dep_block is not None and dep_block.id != block.id:
                block.depends_on.append(dep_block)
    db.flush()
    return template


def generate_or_reuse_template(db: Session, client: Client) -> ProductionStageTemplate:
    existing = _find_existing_template(db, client.house_model_key)
    if existing is not None:
        return existing

    extraction = kr_extraction.get_kr_extraction(db, client.id)
    if extraction is None or not extraction.pages:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Сначала запустите постраничный разбор КР этого клиента",
        )

    if not settings.anthropic_api_key:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Нужен ключ ИИ (ANTHROPIC_API_KEY) для первой генерации шаблона графа этапов",
        )

    pages_payload = _kr_payload(extraction)
    if not pages_payload:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="В разборе КР нет текста ни на одной странице")

    graph = _call_ai(pages_payload)
    return _persist_draft(db, client, graph)


def get_template_or_404(db: Session, template_id: int) -> ProductionStageTemplate:
    template = db.get(ProductionStageTemplate, template_id)
    if template is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Шаблон не найден")
    return template


def get_block_or_404(db: Session, template_id: int, block_id: int) -> TemplateBlock:
    block = db.get(TemplateBlock, block_id)
    if block is None or block.template_id != template_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Блок шаблона не найден")
    return block


def get_task_or_404(db: Session, block_id: int, task_id: int) -> TemplateBlockTask:
    task = db.get(TemplateBlockTask, task_id)
    if task is None or task.template_block_id != block_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Задача шаблона не найдена")
    return task


def get_material_or_404(db: Session, block_id: int, material_id: int) -> TemplateBlockMaterial:
    material = db.get(TemplateBlockMaterial, material_id)
    if material is None or material.template_block_id != block_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Материал шаблона не найден")
    return material


def _require_editable(template: ProductionStageTemplate) -> None:
    if template.status == TemplateStatus.CONFIRMED:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Шаблон уже подтверждён — правки недоступны")


def _mark_reviewed(db: Session, template: ProductionStageTemplate) -> None:
    if template.status == TemplateStatus.DRAFT:
        template.status = TemplateStatus.REVIEWED
    db.flush()


def update_block(db: Session, template: ProductionStageTemplate, block: TemplateBlock, payload) -> TemplateBlock:
    _require_editable(template)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(block, field, value)
    _mark_reviewed(db, template)
    return block


def update_task(db: Session, template: ProductionStageTemplate, task: TemplateBlockTask, payload) -> TemplateBlockTask:
    _require_editable(template)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(task, field, value)
    _mark_reviewed(db, template)
    return task


def update_material(
    db: Session, template: ProductionStageTemplate, material: TemplateBlockMaterial, payload
) -> TemplateBlockMaterial:
    _require_editable(template)
    data = payload.model_dump(exclude_unset=True)
    if "warehouse_material_id" in data and data["warehouse_material_id"] is not None:
        from app.warehouse.models import WarehouseMaterial

        if db.get(WarehouseMaterial, data["warehouse_material_id"]) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Материал склада не найден")
    for field, value in data.items():
        setattr(material, field, value)
    if "warehouse_material_id" in data:
        if data["warehouse_material_id"] is not None:
            # Человек выбрал/поправил сопоставление вручную — это всегда
            # надёжнее ИИ, поэтому запоминаем без "требует проверки" и
            # переиспользуем при следующей генерации (0073-a), без
            # повторного обращения к ИИ по этой же паре имя/ед.
            material.confidence = MappingConfidence.HIGH
            material_matching.record_human_match(db, material.name, material.unit, data["warehouse_material_id"])
        else:
            material.confidence = None
    _mark_reviewed(db, template)
    return material


def confirm_template(db: Session, template: ProductionStageTemplate, user) -> ProductionStageTemplate:
    _require_editable(template)
    template.status = TemplateStatus.CONFIRMED
    template.confirmed_at = datetime.now(timezone.utc)
    template.confirmed_by_id = user.id
    db.flush()
    return template
