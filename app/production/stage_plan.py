"""Применение подтверждённого шаблона графа этапов (0066-d) к реальному
`Production` (0066-f) — превращает `TemplateBlock`/`TemplateBlockTask`/
`TemplateBlockMaterial` в настоящие `ProductionBlock`/`Task`/`BlockMaterial`,
с теми же зависимостями. Вызывается автоматически при переходе цикла клиента
со стадии «оплата» на «постоплата» (`app/clients/service.py::transition_stage`).

Идемпотентно: если у `Production` уже есть блоки — повторный вызов ничего
не создаёт (защита от двойной инстанциации одного и того же производства).
"""

from __future__ import annotations

from app.common.module_access import Module as AccessModule
from app.production.models import BlockMaterial, Production, ProductionBlock
from app.production.stage_templates import ProductionStageTemplate, TemplateBlock
from app.tasks import service as task_service
from app.tasks.models import Task, TaskLinkType
from app.users import service as user_service


def _topological_order(blocks: list[TemplateBlock]) -> list[TemplateBlock]:
    """Зависимости раньше зависимых, `sequence` — тай-брейк внутри уровня.
    Шаблоны уже проверены на отсутствие циклов на уровне графа блоков
    производства (0066-a); здесь достаточно простого Кана."""
    remaining = {b.id: b for b in blocks}
    indegree = {b.id: len(b.depends_on) for b in blocks}
    ordered: list[TemplateBlock] = []
    while remaining:
        ready = sorted(
            (b for b in remaining.values() if indegree[b.id] == 0),
            key=lambda b: b.sequence,
        )
        if not ready:
            # Цикл в данных шаблона (не должен возникать — 0066-a защищает
            # граф от циклов на уровне реальных блоков, здесь на всякий
            # случай не зависаем, а достраиваем оставшееся по sequence).
            ready = sorted(remaining.values(), key=lambda b: b.sequence)
        for block in ready:
            ordered.append(block)
            del remaining[block.id]
            del indegree[block.id]
        for block in remaining.values():
            indegree[block.id] = sum(1 for dep in block.depends_on if dep.id in remaining)
    return ordered


def instantiate_stage_plan(db, production: Production, template: ProductionStageTemplate) -> None:
    # Прямой запрос, а не production.blocks: с expire_on_commit=False (см.
    # tests/conftest.py) закешированная пустая коллекция не обновилась бы
    # сама между двумя вызовами в одной и той же сессии.
    already_instantiated = (
        db.query(ProductionBlock.id).filter(ProductionBlock.production_id == production.id).first()
    )
    if already_instantiated is not None:
        return  # уже инстанциировано на этом производстве — идемпотентность

    assignee_ids = [u.id for u in user_service.users_with_access(db, AccessModule.PRODUCTION)]

    block_by_template_id: dict[int, ProductionBlock] = {}
    tasks_by_template_block_id: dict[int, list[Task]] = {}

    for template_block in _topological_order(list(template.blocks)):
        block = ProductionBlock(
            production_id=production.id,
            name=template_block.name,
            description=template_block.description,
            sequence=template_block.sequence,
        )
        db.add(block)
        db.flush()
        block.depends_on = [block_by_template_id[dep.id] for dep in template_block.depends_on]
        block_by_template_id[template_block.id] = block

        prerequisite_task_ids = [
            task.id
            for dep in template_block.depends_on
            for task in tasks_by_template_block_id.get(dep.id, [])
        ]

        block_tasks: list[Task] = []
        for template_task in template_block.tasks:
            page_number = (template_task.kr_page_ref or {}).get("page_number")
            task = task_service.create_task(
                db,
                title=template_task.title,
                description=template_task.description,
                assignee_ids=assignee_ids,
                depends_on_ids=prerequisite_task_ids,
                block_id=block.id,
                link_type=TaskLinkType.NONE,
                link_meta={"kr_page": page_number} if page_number is not None else None,
            )
            block_tasks.append(task)
        tasks_by_template_block_id[template_block.id] = block_tasks

        for template_material in template_block.materials:
            if template_material.warehouse_material_id is None:
                # КР не даёт однозначного сопоставления с каталогом склада —
                # BlockMaterial требует warehouse_material_id, выдумывать его
                # нельзя. Заводим задачу, чтобы информация не терялась молча,
                # инженер сопоставляет и добавляет материал вручную (как и
                # сегодня для любого материала блока).
                task_service.create_task(
                    db,
                    title=f"Сопоставить со складом материал «{template_material.name}» ({template_material.unit}) и добавить в блок «{block.name}»",
                    assignee_ids=assignee_ids,
                    block_id=block.id,
                )
                continue
            db.add(
                BlockMaterial(
                    block_id=block.id,
                    warehouse_material_id=template_material.warehouse_material_id,
                    inventory_number="",
                    unit=template_material.unit,
                    # Количество не «угадывается» по тексту КР — структурного
                    # распознавания количеств по спецификации конкретного
                    # заказа в проекте пока нет (0057-a закрыта без
                    # реализации); инженер донаполняет вручную, как и любой
                    # материал блока сегодня (update_required_quantity).
                    quantity_required=0,
                )
            )

    db.flush()
