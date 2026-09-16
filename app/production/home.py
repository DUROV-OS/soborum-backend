"""Агрегатор вкладки «Главная» одного производства (0065-a): те же сигналы,
что «Требует внимания» / «Актуальное» на «Пульсе» (`app.dashboard.overview`,
`app.dashboard.aktualnoe`), но пересчитанные по одному циклу/дому, а не по
всей компании — плюс урезанный набор документов клиента.

Право `production` не должно раскрывать цену/контакты/адрес клиента
(см. `app/production/router.py`), поэтому документы собираются вручную из
нужных четырёх полей `Client`, а не через `ClientOut` целиком.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.cycle.models import Cycle
from app.dashboard.aktualnoe import _stage_of
from app.production.deadlines import generate_deadline_insight
from app.production.models import (
    MaterialRequest,
    MaterialRequestStatus,
    ModuleMaterial,
    Production,
    ProductionModule,
)
from app.production.schemas import (
    ProductionAktualnoeOut,
    ProductionAttentionOut,
    ProductionHomeDocumentsOut,
    ProductionHomeOut,
)
from app.tasks.models import Task, TaskStatus


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _module_ids(db: Session, production: Production) -> list[int]:
    return [
        module_id
        for (module_id,) in db.query(ProductionModule.id)
        .filter(ProductionModule.production_id == production.id)
        .all()
    ]


def build_attention(db: Session, production: Production) -> list[ProductionAttentionOut]:
    """Сигналы внимания конкретно этого производства — та же логика, что
    `dashboard/service.py::_snapshot_production` и `dashboard/overview.py`
    (`pending_material_requests`, недостача материалов, просроченные задачи),
    но с фильтром по модулям одного производства вместо всей БД."""
    module_ids = _module_ids(db, production)
    if not module_ids:
        return []

    actions: list[ProductionAttentionOut] = []

    pending_requests = (
        db.query(MaterialRequest)
        .join(ModuleMaterial, MaterialRequest.module_material_id == ModuleMaterial.id)
        .filter(
            ModuleMaterial.module_id.in_(module_ids),
            MaterialRequest.status == MaterialRequestStatus.PENDING,
        )
        .count()
    )
    if pending_requests:
        actions.append(
            ProductionAttentionOut(
                id="production:pending_material_requests",
                title="Проверить заявки на материалы",
                description="Заявки этого дома ожидают решения склада.",
                href="/production",
                tone="warning",
            )
        )

    shortfall = (
        db.query(ModuleMaterial)
        .filter(
            ModuleMaterial.module_id.in_(module_ids),
            (ModuleMaterial.quantity_required > 0) | (ModuleMaterial.quantity_requested > 0),
        )
        .count()
    )
    if shortfall:
        actions.append(
            ProductionAttentionOut(
                id="production:material_shortfall",
                title="Материала не хватает",
                description="Есть материалы модулей, которые ещё не выданы полностью.",
                href="/production",
                tone="warning",
            )
        )

    now = datetime.now(timezone.utc)
    open_tasks_with_deadline = (
        db.query(Task)
        .filter(
            Task.module_id.in_(module_ids),
            Task.status != TaskStatus.DONE,
            Task.deadline.isnot(None),
        )
        .all()
    )
    if any(_aware(task.deadline) < now for task in open_tasks_with_deadline):
        actions.append(
            ProductionAttentionOut(
                id="production:overdue_tasks",
                title="Проверить просроченные задачи",
                description="По модулям этого дома есть задачи с истёкшим сроком.",
                href="/tasks",
                tone="danger",
            )
        )

    return actions


def build_aktualnoe(cycle: Cycle) -> ProductionAktualnoeOut | None:
    """Статус конкретно этого цикла — детерминированный (переиспользует
    `dashboard/aktualnoe._stage_of`, ту же логику, что даёт запасной процент
    на «Пульсе» без ИИ). Единственный цикл, а не топ-N по компании — ранжировать
    нечего, поэтому без ИИ-вызова."""
    client = cycle.client
    if client is None:
        return None
    stage_label, percent = _stage_of(cycle, client)
    return ProductionAktualnoeOut(stage=stage_label, percent=percent, phrase="")


def build_documents(cycle: Cycle) -> ProductionHomeDocumentsOut:
    client = cycle.client
    if client is None:
        return ProductionHomeDocumentsOut(
            house_model=None, ar_file=None, kr_file=None, house_project_file=None
        )
    return ProductionHomeDocumentsOut(
        house_model=client.house_model,
        ar_file=client.ar_file,
        kr_file=client.kr_file,
        house_project_file=client.house_project_file,
    )


def build_home(db: Session, production: Production, force_deadlines: bool = False) -> ProductionHomeOut:
    cycle = production.cycle
    return ProductionHomeOut(
        actions=build_attention(db, production),
        aktualnoe=build_aktualnoe(cycle),
        deadlines=generate_deadline_insight(db, production, force=force_deadlines),
        documents=build_documents(cycle),
    )
