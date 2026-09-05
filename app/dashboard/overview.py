"""Deterministic operational overview. No model-generated counts or stale permission cache."""
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.ai.models import Chat, PendingAction, PendingActionStatus
from app.common.module_access import Module
from app.core.config import settings
from app.dashboard.schemas import DashboardAction, DashboardWidget, TodayDashboardOut
from app.dashboard.service import build_snapshot
from app.users.models import User

# section, title, metric key, attention metric (a positive count needs attention)
METRICS = [
    ("clients", "Клиентов", "total_clients", False),
    ("clients", "Ожидают подтверждения оплаты", "awaiting_payment_confirmation", True),
    ("production", "Производственных заказов", "total_productions", False),
    ("production", "Модули ждут материалы", "modules_with_material_shortfall", True),
    ("installation", "Монтажей на 7 дней", "scheduled_next_7_days", False),
    ("installation", "Монтажей с просрочкой", "overdue_not_completed", True),
    ("cycle", "Всего заказов", "total_cycles", False),
    ("warehouse", "Позиций на складе", "total_materials", False),
    ("warehouse", "Позиций требуют пополнения", "materials_needing_supply", True),
    ("marketing", "Публикаций на 7 дней", "release_due_next_7_days", False),
    ("marketing", "Публикаций с просрочкой", "release_overdue", True),
    ("tasks", "Открытых задач", "open_tasks", False),
    ("tasks", "Просроченных задач", "overdue_tasks", True),
    ("users", "Активных сотрудников", "active_employees", False),
    ("users", "Сотрудников без доступа", "workers_without_module_access", True),
]

# Ordered by urgency, with labels describing what the facts actually establish.
ATTENTION = [
    ("tasks", "overdue_tasks", "Проверить просроченные задачи", "Уточните причину задержки и следующий срок.", "/tasks", "danger"),
    ("installation", "overdue_not_completed", "Проверить сроки монтажа", "Плановая дата прошла, этап проработки ещё не наступил.", "/montage", "danger"),
    ("production", "pending_material_requests", "Проверить заявки на материалы", "Заявки ожидают решения склада.", "/production", "warning"),
    ("warehouse", "materials_needing_supply", "Проверить пополнение склада", "Остатки и текущая потребность требуют внимания.", "/warehouse", "warning"),
    ("clients", "awaiting_payment_confirmation", "Проверить поступление оплаты", "Клиенты на этапе оплаты без подтверждённого поступления.", "/clients", "warning"),
    ("clients", "leads_stuck_over_14_days", "Вернуться к зависшим обращениям", "Обращения остаются на этапе лида больше 14 дней.", "/clients", "warning"),
    ("marketing", "release_overdue", "Проверить план публикаций", "Плановая дата прошла, материалы ещё не выпущены.", "/marketing", "warning"),
    ("users", "workers_without_module_access", "Назначить доступ сотрудникам", "Активным сотрудникам не выдан доступ к рабочим разделам.", "/admin", "warning"),
]


def generate_today(db: Session, user: User) -> TodayDashboardOut:
    snapshot = build_snapshot(db, user)
    widgets = []
    for section, title, metric, attention in METRICS:
        if section not in snapshot:
            continue
        value = int(snapshot[section][metric])
        widgets.append(DashboardWidget(
            section=section, title=title, value=str(value),
            tone=("warning" if value else "success") if attention else "neutral",
        ))
    actions = [
        DashboardAction(id=f"{section}:{metric}", section=section, title=title,
                        description=description, href=href, count=int(snapshot[section][metric]), tone=tone)
        for section, metric, title, description, href, tone in ATTENTION
        if section in snapshot and snapshot[section][metric] > 0
    ]
    if user.has_access(Module.AI):
        pending = (db.query(PendingAction).join(Chat, Chat.id == PendingAction.chat_id)
                   .filter(Chat.owner_id == user.id, PendingAction.status == PendingActionStatus.PENDING)
                   .order_by(PendingAction.id).all())
        if pending:
            actions.insert(0, DashboardAction(
                id="ai:approvals", section="ai", title="Рассмотреть действия Марины",
                description="Проверьте предложенные изменения перед выполнением.",
                href=f"/ai/{pending[0].chat_id}", count=len(pending), tone="warning",
            ))
    summary = (
        f"Направлений, требующих внимания: {len(actions)}. Начните с очереди ниже."
        if actions else "По доступным данным отклонений для очереди внимания нет."
    ) if snapshot else "Рабочие разделы пока не назначены. Обратитесь к администратору."
    return TodayDashboardOut(generated_at=datetime.now(timezone.utc), summary=summary,
                             widgets=widgets, actions=actions,
                             ai_configured=user.has_access(Module.AI) and bool(settings.anthropic_api_key))
