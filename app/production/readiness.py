"""Оценка готовности производства и блока (0084-b) — единственное место, где
считается, обеспечены ли блоки материалами и допущены ли они к работе.

Раньше каждый потребитель (Пульс, «Главная» производства, дедлайны, Марина)
считал «нехватку» своим условием, и ни одно не видело отсутствие данных:
блок без материалов или с количеством 0 выглядел благополучным. Здесь
отсутствие данных — отдельное, худшее состояние.

Два независимых признака блока:

- `materials_state` — состояние материалов, одно из `MaterialsState`;
- `admitted` — блок допущен: все блоки из `depends_on` закрыты, то есть у
  каждого есть задачи и все они в DONE. Блок-зависимость без задач закрытым
  не считается — закрывать в нём нечего, и «допуск» по нему был бы выдуман.

Ограничение P0: складской остаток (чужие резервы, доступное количество) не
учитывается. «Материалы обеспечены» значит «всё, что указано в блоке,
выдано по заявкам со склада», а не «на складе хватает». Сопоставление с
остатком — P1 (п.5 gpt_prototype#37).

Модуль только читает: ничего не пишет в БД и не вызывает LLM.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload

from app.production.models import BlockMaterial, Production, ProductionBlock
from app.tasks.models import Task, TaskLinkType, TaskStageEvent, TaskStatus

READINESS_VERSION = "readiness-v1"


class MaterialsState(str, enum.Enum):
    # Порядок объявления — от худшего к лучшему; состояние производства —
    # худшее из состояний его блоков (см. `worst_state`).
    INSUFFICIENT_DATA = "insufficient_data"
    NEEDS_RECONCILIATION = "needs_reconciliation"
    SHORTFALL = "shortfall"
    PROVIDED = "provided"
    NOT_REQUIRED = "not_required"


STATE_LABELS: dict[MaterialsState, str] = {
    MaterialsState.INSUFFICIENT_DATA: "Недостаточно данных",
    MaterialsState.NEEDS_RECONCILIATION: "Нужна сверка",
    MaterialsState.SHORTFALL: "Нехватка материалов",
    MaterialsState.PROVIDED: "Материалы обеспечены",
    MaterialsState.NOT_REQUIRED: "Материалы не требуются",
}

_SEVERITY = {state: index for index, state in enumerate(MaterialsState)}

# Состояния, при которых по материалам есть что делать человеку.
PROBLEM_STATES = frozenset(
    {MaterialsState.INSUFFICIENT_DATA, MaterialsState.NEEDS_RECONCILIATION, MaterialsState.SHORTFALL}
)


def worst_state(states) -> MaterialsState | None:
    states = list(states)
    if not states:
        return None
    return min(states, key=lambda state: _SEVERITY[state])


@dataclass
class ReadinessReason:
    code: str
    text: str
    block_id: int | None = None
    material_id: int | None = None
    task_id: int | None = None


@dataclass
class ReadinessSources:
    """Какие записи прочитаны при расчёте — чтобы оценку можно было проверить."""

    block_ids: list[int] = field(default_factory=list)
    material_ids: list[int] = field(default_factory=list)
    task_ids: list[int] = field(default_factory=list)
    material_request_ids: list[int] = field(default_factory=list)


@dataclass
class WaitingOn:
    block_id: int
    name: str


@dataclass
class BlockReadiness:
    block_id: int
    production_id: int
    name: str
    materials_state: MaterialsState
    admitted: bool
    waiting_on: list[WaitingOn]
    reasons: list[ReadinessReason]
    sources: ReadinessSources
    computed_at: datetime
    facts_at: datetime | None
    version: str = READINESS_VERSION

    @property
    def materials_label(self) -> str:
        return STATE_LABELS[self.materials_state]


@dataclass
class ProductionReadiness:
    production_id: int
    materials_state: MaterialsState
    reasons: list[ReadinessReason]
    sources: ReadinessSources
    computed_at: datetime
    facts_at: datetime | None
    blocks: list[BlockReadiness]
    version: str = READINESS_VERSION

    @property
    def materials_label(self) -> str:
        return STATE_LABELS[self.materials_state]

    @property
    def problem_reasons(self) -> list[ReadinessReason]:
        """Причины только от блоков в проблемных состояниях — без пояснений
        к «обеспечены» / «не требуются»."""
        problem_block_ids = {b.block_id for b in self.blocks if b.materials_state in PROBLEM_STATES}
        return [
            r for r in self.reasons
            if r.block_id in problem_block_ids or (r.block_id is None and self.materials_state in PROBLEM_STATES)
        ]


# ------------------------------------------------------------------ расчёт --


def _utc(value: datetime | None) -> datetime | None:
    # SQLite в тестах отдаёт наивное время (UTC), PostgreSQL — с зоной;
    # сравнивать их между собой нельзя.
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _latest(*values: datetime | None) -> datetime | None:
    present = [v for v in (_utc(v) for v in values) if v is not None]
    return max(present) if present else None


def _qty(value) -> str:
    number = float(value or 0)
    return f"{number:g}"


@dataclass
class _BlockTask:
    id: int
    status: TaskStatus
    link_type: TaskLinkType
    title: str


def _is_zero(value) -> bool:
    return float(value or 0) == 0


def _material_title(material: BlockMaterial) -> str:
    warehouse_material = material.warehouse_material
    return warehouse_material.title if warehouse_material is not None else f"материал #{material.warehouse_material_id}"


def _plural_materials(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return "материала"
    return "материалов"


def _assess_block(
    block: ProductionBlock,
    tasks_by_block: dict[int, list[_BlockTask]],
    events_at_by_block: dict[int, datetime],
    computed_at: datetime,
) -> BlockReadiness:
    label = f"Блок «{block.name}»"
    materials = sorted(block.materials, key=lambda m: m.id)
    tasks = tasks_by_block.get(block.id, [])

    sources = ReadinessSources(
        block_ids=[block.id],
        material_ids=[m.id for m in materials],
        task_ids=[t.id for t in tasks],
        material_request_ids=sorted(r.id for m in materials for r in m.requests),
    )
    facts_at = _latest(
        block.updated_at,
        events_at_by_block.get(block.id),
        *(m.updated_at for m in materials),
        *(r.created_at for m in materials for r in m.requests),
        *(r.decided_at for m in materials for r in m.requests),
    )

    waiting_on = []
    for dependency in sorted(block.depends_on, key=lambda b: (b.sequence, b.id)):
        dependency_tasks = tasks_by_block.get(dependency.id, [])
        closed = bool(dependency_tasks) and all(t.status == TaskStatus.DONE for t in dependency_tasks)
        if not closed:
            waiting_on.append(WaitingOn(block_id=dependency.id, name=dependency.name))

    reasons: list[ReadinessReason] = []
    open_matches = [t for t in tasks
                    if t.link_type == TaskLinkType.BLOCK_MATERIAL_MATCH and t.status != TaskStatus.DONE]
    match_reasons = [
        ReadinessReason(
            code="material_match_open",
            text=f"{label}: материал не сопоставлен со складом — открыта задача «{task.title}»",
            block_id=block.id,
            task_id=task.id,
        )
        for task in open_matches
    ]

    if not block.requires_materials:
        state = MaterialsState.NOT_REQUIRED
        reasons.append(ReadinessReason(
            code="not_required", text=f"{label}: помечен «материалы не требуются»", block_id=block.id,
        ))
    elif not materials:
        state = MaterialsState.INSUFFICIENT_DATA
        reasons.append(ReadinessReason(
            code="no_materials", text=f"{label}: материалы не указаны", block_id=block.id,
        ))
        reasons.extend(match_reasons)
    else:
        unset = [m for m in materials
                 if _is_zero(m.quantity_required) and _is_zero(m.quantity_requested) and _is_zero(m.quantity_provided)]
        not_requested = [m for m in materials if float(m.quantity_required or 0) > 0]
        awaiting = [m for m in materials if float(m.quantity_requested or 0) > 0]

        if unset:
            reasons.append(ReadinessReason(
                code="quantity_not_set",
                text=f"{label}: у {len(unset)} {_plural_materials(len(unset))} не указано количество",
                block_id=block.id,
                material_id=unset[0].id if len(unset) == 1 else None,
            ))
        reasons.extend(match_reasons)
        for material in not_requested:
            reasons.append(ReadinessReason(
                code="not_requested",
                text=f"{label}: «{_material_title(material)}» — не заказано {_qty(material.quantity_required)} {material.unit}",
                block_id=block.id,
                material_id=material.id,
            ))
        for material in awaiting:
            reasons.append(ReadinessReason(
                code="awaiting_issue",
                text=f"{label}: «{_material_title(material)}» — ждёт выдачи со склада {_qty(material.quantity_requested)} {material.unit}",
                block_id=block.id,
                material_id=material.id,
            ))

        if unset:
            state = MaterialsState.INSUFFICIENT_DATA
        elif open_matches:
            state = MaterialsState.NEEDS_RECONCILIATION
        elif not_requested or awaiting:
            state = MaterialsState.SHORTFALL
        else:
            state = MaterialsState.PROVIDED
            reasons.append(ReadinessReason(
                code="provided_by_requests",
                text=f"{label}: всё указанное выдано по заявкам; складской остаток не сверялся",
                block_id=block.id,
            ))

    return BlockReadiness(
        block_id=block.id,
        production_id=block.production_id,
        name=block.name,
        materials_state=state,
        admitted=not waiting_on,
        waiting_on=waiting_on,
        reasons=reasons,
        sources=sources,
        computed_at=computed_at,
        facts_at=facts_at,
    )


def _assess_many(db: Session, production_ids: list[int]) -> dict[int, ProductionReadiness]:
    computed_at = datetime.now(timezone.utc)
    if not production_ids:
        return {}

    blocks = (
        db.query(ProductionBlock)
        .filter(ProductionBlock.production_id.in_(production_ids))
        .options(
            selectinload(ProductionBlock.materials).selectinload(BlockMaterial.requests),
            selectinload(ProductionBlock.materials).selectinload(BlockMaterial.warehouse_material),
            selectinload(ProductionBlock.depends_on),
        )
        .order_by(ProductionBlock.sequence, ProductionBlock.id)
        .all()
    )
    block_ids = [b.id for b in blocks]

    tasks_by_block: dict[int, list[_BlockTask]] = {}
    events_at_by_block: dict[int, datetime] = {}
    if block_ids:
        rows = (
            db.query(Task.id, Task.block_id, Task.status, Task.link_type, Task.title)
            .filter(Task.block_id.in_(block_ids))
            .order_by(Task.id)
            .all()
        )
        for task_id, block_id, status, link_type, title in rows:
            tasks_by_block.setdefault(block_id, []).append(
                _BlockTask(id=task_id, status=status, link_type=link_type, title=title)
            )
        # Последний переход задач блока (включая создание) — тоже «факт».
        for block_id, latest in (
            db.query(Task.block_id, func.max(TaskStageEvent.created_at))
            .join(TaskStageEvent, TaskStageEvent.task_id == Task.id)
            .filter(Task.block_id.in_(block_ids))
            .group_by(Task.block_id)
            .all()
        ):
            if latest is not None:
                events_at_by_block[block_id] = latest

    by_production: dict[int, list[BlockReadiness]] = {pid: [] for pid in production_ids}
    for block in blocks:
        by_production[block.production_id].append(
            _assess_block(block, tasks_by_block, events_at_by_block, computed_at)
        )

    result: dict[int, ProductionReadiness] = {}
    for production_id, block_assessments in by_production.items():
        if not block_assessments:
            result[production_id] = ProductionReadiness(
                production_id=production_id,
                materials_state=MaterialsState.INSUFFICIENT_DATA,
                reasons=[ReadinessReason(code="no_blocks", text="У производства нет ни одного блока")],
                sources=ReadinessSources(),
                computed_at=computed_at,
                facts_at=None,
                blocks=[],
            )
            continue
        # Причины — от худших блоков к лучшим, внутри — в порядке блоков.
        ordered = sorted(block_assessments, key=lambda b: _SEVERITY[b.materials_state])
        result[production_id] = ProductionReadiness(
            production_id=production_id,
            materials_state=worst_state(b.materials_state for b in block_assessments),
            reasons=[reason for b in ordered for reason in b.reasons],
            sources=ReadinessSources(
                block_ids=[i for b in block_assessments for i in b.sources.block_ids],
                material_ids=[i for b in block_assessments for i in b.sources.material_ids],
                task_ids=[i for b in block_assessments for i in b.sources.task_ids],
                material_request_ids=[i for b in block_assessments for i in b.sources.material_request_ids],
            ),
            computed_at=computed_at,
            facts_at=_latest(*(b.facts_at for b in block_assessments)),
            blocks=block_assessments,
        )
    return result


def assess_production(db: Session, production: Production) -> ProductionReadiness:
    return _assess_many(db, [production.id])[production.id]


def assess_productions(db: Session, productions: list[Production]) -> list[ProductionReadiness]:
    """Оценка сразу нескольких производств — тем же числом запросов, что и
    одного (для списка производств и Пульса)."""
    assessments = _assess_many(db, [p.id for p in productions])
    return [assessments[p.id] for p in productions]


def assess_block(db: Session, block: ProductionBlock) -> BlockReadiness:
    production = _assess_many(db, [block.production_id])[block.production_id]
    return next(b for b in production.blocks if b.block_id == block.id)
