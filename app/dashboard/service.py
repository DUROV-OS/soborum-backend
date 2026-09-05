"""Permission-scoped database snapshots shared by Today and optional AI analytics."""

from datetime import date, datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.clients.models import Client, ClientStage
from app.common.module_access import Module
from app.cycle.models import Cycle, CycleStatus
from app.installation.models import Installation, InstallationStage
from app.marketing.models import ContentItem, ContentStage
from app.production.models import MaterialRequest, MaterialRequestStatus, ModuleMaterial, Production, ProductionModule
from app.tasks.models import Task, TaskStatus
from app.users.models import User, UserRole
from app.warehouse import service as warehouse_service
from app.warehouse.models import Supply

SECTION_LABELS: dict[str, str] = {
    "clients": "Клиенты",
    "production": "Производство",
    "installation": "Монтаж",
    "cycle": "Цикл клиента",
    "warehouse": "Склад",
    "marketing": "Маркетинг",
    "tasks": "Задачи",
    "users": "Сотрудники",
}


# ------------------------------------------------------------- snapshots --

def _snapshot_clients(db: Session) -> dict:
    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)
    two_weeks_ago = now - timedelta(days=14)

    stage_counts = dict.fromkeys([s.value for s in ClientStage], 0)
    for stage, count in db.query(Client.stage, func.count(Client.id)).group_by(Client.stage).all():
        stage_counts[stage.value] = count

    return {
        "total_clients": sum(stage_counts.values()),
        "stage_counts": stage_counts,
        "awaiting_payment_confirmation": db.query(Client)
        .filter(Client.stage == ClientStage.PAYMENT, Client.is_paid.isnot(True))
        .count(),
        "new_leads_last_7_days": db.query(Client)
        .filter(Client.stage == ClientStage.LEAD, Client.created_at >= week_ago)
        .count(),
        "leads_stuck_over_14_days": db.query(Client)
        .filter(Client.stage == ClientStage.LEAD, Client.created_at < two_weeks_ago)
        .count(),
    }


def _snapshot_cycle(db: Session) -> dict:
    status_counts = dict.fromkeys([s.value for s in CycleStatus], 0)
    for cycle_status, count in db.query(Cycle.status, func.count(Cycle.id)).group_by(Cycle.status).all():
        status_counts[cycle_status.value] = count
    return {"total_cycles": sum(status_counts.values()), "status_counts": status_counts}


def _snapshot_installation(db: Session) -> dict:
    today = date.today()
    week_ahead = today + timedelta(days=7)

    stage_counts = dict.fromkeys([s.value for s in InstallationStage], 0)
    for stage, count in db.query(Installation.stage, func.count(Installation.id)).group_by(Installation.stage).all():
        stage_counts[stage.value] = count

    return {
        "total_installations": sum(stage_counts.values()),
        "stage_counts": stage_counts,
        "scheduled_next_7_days": db.query(Installation)
        .filter(
            Installation.scheduled_date.isnot(None),
            Installation.scheduled_date >= today,
            Installation.scheduled_date <= week_ahead,
        )
        .count(),
        "overdue_not_completed": db.query(Installation)
        .filter(
            Installation.scheduled_date.isnot(None),
            Installation.scheduled_date < today,
            Installation.stage != InstallationStage.FOLLOWUP,
        )
        .count(),
        "unscheduled": db.query(Installation).filter(Installation.scheduled_date.is_(None)).count(),
    }


def _snapshot_marketing(db: Session) -> dict:
    today = date.today()
    week_ahead = today + timedelta(days=7)
    not_released = [ContentStage.IDEA, ContentStage.GATHERING, ContentStage.EDITING]

    stage_counts = dict.fromkeys([s.value for s in ContentStage], 0)
    for stage, count in db.query(ContentItem.stage, func.count(ContentItem.id)).group_by(ContentItem.stage).all():
        stage_counts[stage.value] = count

    return {
        "total_content_items": sum(stage_counts.values()),
        "stage_counts": stage_counts,
        "release_due_next_7_days": db.query(ContentItem)
        .filter(
            ContentItem.stage.in_(not_released),
            ContentItem.planned_release_date.isnot(None),
            ContentItem.planned_release_date >= today,
            ContentItem.planned_release_date <= week_ahead,
        )
        .count(),
        "release_overdue": db.query(ContentItem)
        .filter(
            ContentItem.stage.in_(not_released),
            ContentItem.planned_release_date.isnot(None),
            ContentItem.planned_release_date < today,
        )
        .count(),
    }


def _snapshot_production(db: Session) -> dict:
    shortfall_module_ids = {
        row[0]
        for row in db.query(ModuleMaterial.module_id)
        .filter((ModuleMaterial.quantity_required > 0) | (ModuleMaterial.quantity_requested > 0))
        .distinct()
        .all()
    }
    return {
        "total_productions": db.query(Production).count(),
        "total_modules": db.query(ProductionModule).count(),
        "modules_with_material_shortfall": len(shortfall_module_ids),
        "pending_material_requests": db.query(MaterialRequest)
        .filter(MaterialRequest.status == MaterialRequestStatus.PENDING)
        .count(),
    }


def _snapshot_warehouse(db: Session) -> dict:
    materials = warehouse_service.list_materials(db)
    needs_supply = [m for m in materials if m.needs_supply]
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    top_shortage = sorted(needs_supply, key=lambda m: m.quantity_in_stock - m.threshold)[:5]
    return {
        "total_materials": len(materials),
        "materials_needing_supply": len(needs_supply),
        "supplies_recorded_last_7_days": db.query(Supply).filter(Supply.created_at >= week_ago).count(),
        "top_shortage_materials": [
            {
                "title": m.title,
                "warehouse": m.warehouse.value,
                "quantity_in_stock": m.quantity_in_stock,
                "threshold": m.threshold,
            }
            for m in top_shortage
        ],
    }


def _snapshot_tasks(db: Session) -> dict:
    now = datetime.now(timezone.utc)
    status_counts = dict.fromkeys([s.value for s in TaskStatus], 0)
    for task_status, count in db.query(Task.status, func.count(Task.id)).group_by(Task.status).all():
        status_counts[task_status.value] = count

    open_tasks = db.query(Task).filter(Task.status != TaskStatus.DONE).all()
    overdue = sum(1 for t in open_tasks if t.deadline and (t.deadline.replace(tzinfo=timezone.utc) if t.deadline.tzinfo is None else t.deadline) < now)
    due_today = sum(1 for t in open_tasks if t.deadline and t.deadline.date() == now.date())

    return {
        "total_tasks": sum(status_counts.values()),
        "status_counts": status_counts,
        "open_tasks": len(open_tasks),
        "overdue_tasks": overdue,
        "due_today": due_today,
    }


def _snapshot_users(db: Session) -> dict:
    active_users = db.query(User).filter(User.is_active.is_(True)).all()
    workers = [u for u in active_users if u.role == UserRole.WORKER]

    workload = []
    for u in workers:
        open_count = (
            db.query(Task)
            .filter(Task.assignees.any(User.id == u.id), Task.status != TaskStatus.DONE)
            .count()
        )
        workload.append({"full_name": u.full_name, "open_tasks": open_count})
    workload.sort(key=lambda w: w["open_tasks"], reverse=True)

    return {
        "active_employees": len(active_users),
        "workers_without_module_access": sum(1 for u in workers if not u.module_access),
        "top_workload": workload[:5],
    }


SECTION_BUILDERS: dict[str, tuple[Module, Callable[[Session], dict]]] = {
    "clients": (Module.CLIENTS, _snapshot_clients),
    "production": (Module.PRODUCTION, _snapshot_production),
    "installation": (Module.INSTALLATION, _snapshot_installation),
    "cycle": (Module.CYCLE, _snapshot_cycle),
    "warehouse": (Module.WAREHOUSE, _snapshot_warehouse),
    "marketing": (Module.MARKETING, _snapshot_marketing),
    "tasks": (Module.TASKS, _snapshot_tasks),
}


def build_snapshot(db: Session, user: User) -> dict[str, dict]:
    snapshot = {key: builder(db) for key, (module, builder) in SECTION_BUILDERS.items() if user.has_access(module)}
    if user.role == UserRole.ADMIN:
        snapshot["users"] = _snapshot_users(db)
    return snapshot


def generate_widgets(db: Session, user: User, force: bool = False):
    # Keep the route and reload parameter compatible; operational facts are never AI-cached.
    from app.dashboard.overview import generate_today
    return generate_today(db, user)
