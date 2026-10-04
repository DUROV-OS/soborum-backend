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
from app.production import kr_extraction
from app.production.stage_templates import (
    ProductionStageTemplate,
    TemplateBlock,
    TemplateBlockMaterial,
    TemplateBlockTask,
    TemplateStatus,
)

_MAX_PAGE_CHARS = 2000
# Страницы спецификации / ведомости материалов (0088-e) — таблица с
# количествами длиннее обычной страницы и при обрезке до 2000 символов теряет
# хвост, а нормативы на дом берутся именно оттуда.
_MAX_SPEC_PAGE_CHARS = 8000
_SPEC_PAGE_MARKERS = ("спецификац", "ведомост", "кол-во", "количество")

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
    "4. Количество материала (quantity) — норматив на ОДИН дом — указывай, "
    "только если оно явно написано в тексте КР (спецификация, ведомость "
    "материалов) у этого материала; бери число как есть, в единицах unit. Не "
    "вычисляй по чертежам и размерам, не суммируй и не угадывай: если явного "
    "числа нет или ты не уверен, что оно относится к этому материалу, — не "
    "указывай quantity вовсе.\n"
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
                                    "quantity": {
                                        "type": "number",
                                        "description": "Норматив на один дом, только если явно указан в КР",
                                    },
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


def _is_spec_page(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _SPEC_PAGE_MARKERS)


def _kr_payload(extraction) -> list[dict]:
    payload = []
    for page in extraction.pages:
        text = (page.get("text") or "").strip()
        if not text:
            continue
        limit = _MAX_SPEC_PAGE_CHARS if _is_spec_page(text) else _MAX_PAGE_CHARS
        payload.append({"page_number": page["page_number"], "text": text[:limit]})
    return payload


def _ai_quantity(raw) -> float | None:
    """Норматив из ответа ИИ: только положительное число. Всё остальное
    (пусто, ноль, текст вроде «по месту») — «в КР не найдено», а не 0."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, str):
        try:
            raw = float(raw.replace(",", ".").strip())
        except ValueError:
            return None
    if isinstance(raw, (int, float)) and raw > 0:
        return float(raw)
    return None


def _forced_tool_call(system: str, payload, tool: dict) -> dict:
    """Один вызов ИИ с принудительным инструментом; ответ без инструмента
    или оборванный по лимиту токенов — явная ошибка, а не тихий частичный результат."""
    from app.core.llm import llm_client

    # Полный граф на реальный многостраничный КР — не короткая структурированная
    # реплика вроде deadlines._ai_pick_bottleneck: генерация с max_tokens=16000
    # не укладывается в дефолтный клиентский timeout (60с, рассчитан на быстрые
    # вызовы) — раньше запрос обрывался клиентом раньше, чем модель успевала
    # дописать JSON. Увеличены оба параметра.
    response = llm_client(timeout=300.0).messages.create(
        model=settings.llm_model,
        max_tokens=16000,
        system=system,
        messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        tools=[tool],
        tool_choice={"type": "tool", "name": tool["name"]},
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
            detail="Ответ ИИ оборван по лимиту токенов — результат неполный, попробуйте ещё раз",
        )
    return tool_use.input


def _call_ai(pages_payload: list[dict]) -> dict:
    """Единственная точка сетевого вызова Claude для генерации графа — вынесена
    отдельно, чтобы тесты монки-патчили именно её (как
    `deadlines._ai_pick_bottleneck`), не поднимая реальную сеть, и считали число вызовов."""
    return _normalize_graph(_forced_tool_call(SYSTEM_PROMPT, pages_payload, TOOL_SCHEMA))


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
            db.add(
                TemplateBlockMaterial(
                    template_block_id=block.id,
                    name=raw_material["name"],
                    unit=raw_material["unit"],
                    kr_page_ref=raw_material.get("kr_page_ref"),
                    quantity=_ai_quantity(raw_material.get("quantity")),
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

    if not settings.llm_configured:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Нужен ключ активного ИИ-провайдера (AI_PROVIDER) для первой генерации шаблона графа этапов",
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
        if field == "requires_materials" and value is None:
            continue  # колонка NOT NULL: null значит «не менять»
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
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(material, field, value)
    _mark_reviewed(db, template)
    return material


def confirm_template(db: Session, template: ProductionStageTemplate, user) -> ProductionStageTemplate:
    _require_editable(template)
    template.status = TemplateStatus.CONFIRMED
    template.confirmed_at = datetime.now(timezone.utc)
    template.confirmed_by_id = user.id
    db.flush()
    return template


# ------------------------------- дозаполнение нормативов из КР (0088-e) --

QUANTITIES_TOOL_NAME = "submit_material_quantities"

QUANTITIES_SYSTEM_PROMPT = (
    "Ты — инженер-технолог производства модульных домов «Soborbum». Тебе передан "
    "JSON с двумя полями: kr_pages — постраничный текст КР (конструктивных "
    "решений) одного дома, {page_number, text}; materials — материалы шаблона "
    "производства этого дома без норматива, {material_id, name, unit, block}.\n\n"
    "Для каждого материала найди в тексте КР явно указанное количество на ОДИН "
    "дом (спецификация, ведомость материалов). СТРОГО следуй правилам:\n"
    "1. Только число, явно написанное в тексте у этого материала, в единицах unit. "
    "Не вычисляй по чертежам и размерам, не суммируй строки, не угадывай.\n"
    "2. Если явного числа нет или нет уверенности, что оно относится именно к "
    "этому материалу, — не включай материал в ответ.\n"
    "3. У каждого найденного количества обязательна ссылка на страницу КР (kr_page_ref).\n"
    "Отвечай ТОЛЬКО вызовом инструмента submit_material_quantities, без текста."
)

QUANTITIES_TOOL_SCHEMA = {
    "name": QUANTITIES_TOOL_NAME,
    "description": "Отправить нормативы на один дом, найденные в тексте КР.",
    "input_schema": {
        "type": "object",
        "properties": {
            "quantities": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "material_id": {"type": "integer"},
                        "quantity": {"type": "number"},
                        "kr_page_ref": {
                            "type": "object",
                            "properties": {
                                "page_number": {"type": "integer"},
                                "note": {"type": "string"},
                            },
                            "required": ["page_number"],
                        },
                    },
                    "required": ["material_id", "quantity", "kr_page_ref"],
                },
            },
        },
        "required": ["quantities"],
    },
}

# Материалов в шаблоне реального дома — сотни; ответ на всех сразу упирается в
# лимит токенов, поэтому спрашиваем порциями.
_QUANTITIES_BATCH = 120


def _call_ai_quantities(pages_payload: list[dict], materials: list[dict]) -> list[dict]:
    """Точка сетевого вызова для дозаполнения нормативов — тесты монки-патчат её."""
    raw = _forced_tool_call(
        QUANTITIES_SYSTEM_PROMPT,
        {"kr_pages": pages_payload, "materials": materials},
        QUANTITIES_TOOL_SCHEMA,
    )
    quantities = raw.get("quantities")
    if isinstance(quantities, str):
        try:
            quantities = json.loads(quantities)
        except json.JSONDecodeError:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="ИИ вернул нераспознаваемый список нормативов")
        if isinstance(quantities, dict):
            quantities = quantities.get("quantities")
    return quantities if isinstance(quantities, list) else []


def fill_quantities_from_kr(db: Session, template: ProductionStageTemplate) -> dict:
    """Проставляет норматив только материалам шаблона, у которых его нет; ничего
    другого в шаблоне не меняет, поэтому допустимо и для подтверждённого шаблона.
    Развёрнутые ранее дома не трогает — их нормативы правятся в производстве дома."""
    missing = [m for block in template.blocks for m in block.materials if m.quantity is None]
    if not missing:
        return {"filled": 0, "remaining": 0}

    if not settings.anthropic_api_key:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Нужен ключ ИИ (ANTHROPIC_API_KEY) для заполнения нормативов из КР",
        )
    extraction = kr_extraction.get_kr_extraction(db, template.source_client_id)
    pages_payload = _kr_payload(extraction) if extraction is not None and extraction.pages else []
    if not pages_payload:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Нет постраничного разбора КР, по которому строился шаблон",
        )

    by_id = {m.id: m for m in missing}
    block_names = {b.id: b.name for b in template.blocks}
    filled = 0
    for start in range(0, len(missing), _QUANTITIES_BATCH):
        batch = [
            {"material_id": m.id, "name": m.name, "unit": m.unit, "block": block_names.get(m.template_block_id)}
            for m in missing[start : start + _QUANTITIES_BATCH]
        ]
        for item in _call_ai_quantities(pages_payload, batch):
            if not isinstance(item, dict):
                continue
            material = by_id.get(item.get("material_id"))
            quantity = _ai_quantity(item.get("quantity"))
            if material is None or quantity is None or material.quantity is not None:
                continue
            material.quantity = quantity
            if material.kr_page_ref is None and isinstance(item.get("kr_page_ref"), dict):
                material.kr_page_ref = item["kr_page_ref"]
            filled += 1
    db.flush()
    return {"filled": filled, "remaining": len(missing) - filled}
