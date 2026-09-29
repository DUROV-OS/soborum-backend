"""Deterministic operational overview. No model-generated counts or stale permission cache."""
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.ai import cache as ai_cache
from app.ai.models import Chat, PendingAction, PendingActionStatus
from app.common.module_access import Module
from app.core.config import settings
from app.dashboard.schemas import DashboardAction, DashboardWidget, SectionSignalOut, TodayDashboardOut
from app.dashboard.service import SECTION_BUILDERS, _snapshot_users, build_snapshot
from app.users.models import User, UserRole

# section, title, metric key, attention metric (a positive count needs attention)
METRICS = [
    ("clients", "Клиентов", "total_clients", False),
    ("clients", "Ожидают подтверждения оплаты", "awaiting_payment_confirmation", True),
    ("clients", "Ожидают оплаты после получения", "awaiting_balance_payment", True),
    ("production", "Производственных заказов", "total_productions", False),
    ("production", "Производств с проблемами по материалам", "productions_needing_attention", True),
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
    ("accounting", "Проводок в реестре", "total_movements", False),
    ("accounting", "Черновиков без согласования", "draft_awaiting_approval", True),
]

# Ordered by urgency, with labels describing what the facts actually establish.
ATTENTION = [
    ("tasks", "overdue_tasks", "Проверить просроченные задачи", "Уточните причину задержки и следующий срок.", "/tasks", "danger"),
    ("cycle", "stuck_over_14_days", "Вернуться к зависшим циклам", "Цикл не продвигается дальше текущей стадии больше 14 дней.", "/cycles", "warning"),
    ("installation", "overdue_not_completed", "Проверить сроки монтажа", "Плановая дата прошла, этап проработки ещё не наступил.", "/montage", "danger"),
    # Раскрывается в пункт на каждое производство (см. _production_readiness_actions).
    ("production", "productions_needing_attention", "Проверить материалы производства", "", "/production", "warning"),
    ("production", "pending_material_requests", "Проверить заявки на материалы", "Заявки ожидают решения склада.", "/production", "warning"),
    ("warehouse", "materials_needing_supply", "Проверить пополнение склада", "Остатки и текущая потребность требуют внимания.", "/warehouse", "warning"),
    ("clients", "awaiting_payment_confirmation", "Проверить поступление оплаты", "Клиенты на этапе оплаты без подтверждённого поступления.", "/clients", "warning"),
    ("clients", "awaiting_balance_payment", "Принять оплату после получения", "Клиенты на постоплате с непогашенным остатком по договору.", "/clients", "warning"),
    ("clients", "leads_stuck_over_14_days", "Вернуться к зависшим обращениям", "Обращения остаются на этапе лида больше 14 дней.", "/clients", "warning"),
    ("marketing", "release_overdue", "Проверить план публикаций", "Плановая дата прошла, материалы ещё не выпущены.", "/marketing", "warning"),
    ("users", "workers_without_module_access", "Назначить доступ сотрудникам", "Активным сотрудникам не выдан доступ к рабочим разделам.", "/admin", "warning"),
    ("accounting", "draft_awaiting_approval", "Проверить черновики проводок", "Проводки заведены, но ещё не согласованы.", "/accounting", "warning"),
]

# Разделы, для которых вообще есть проверка на сигнал внимания — только для них
# "action is None" означает "проверили, проблем нет", а не "не смотрели".
ATTENTION_SECTIONS = {sec for sec, *_ in ATTENTION}

# Текст для зелёного «всё в порядке» — что именно проверили и что там чисто.
# Один на раздел (не на метрику): у раздела может быть несколько ATTENTION-
# записей (например clients — 3), а "action is None" означает, что ни одна
# из них не сработала, то есть чисто по всем сразу.
ALL_CLEAR_TEXT: dict[str, str] = {
    "tasks": "Просроченных задач нет.",
    "cycle": "Зависших циклов нет.",
    "installation": "Просроченных монтажей нет.",
    # Показывается только при благополучной оценке готовности — см.
    # _production_clear_text: «заявок нет» само по себе не значит, что
    # материалов хватает.
    "production": "Всё, что указано в блоках, выдано по заявкам; заявок, ожидающих решения, нет.",
    "warehouse": "Позиций, требующих пополнения, нет.",
    "clients": "Проблемных оплат и зависших обращений нет.",
    "marketing": "Просроченных публикаций нет.",
    "users": "Все активные сотрудники получили доступ.",
    "accounting": "Черновиков без согласования нет.",
}


# Тон по оценке готовности (app/production/readiness.py), а не по «число > 0».
_READINESS_TONE = {
    "insufficient_data": "warning",
    "needs_reconciliation": "warning",
    "shortfall": "danger",
}
_READINESS_METRIC = "productions_needing_attention"


def _readiness_tone(state: str | None) -> str:
    if state is None:
        return "neutral"  # производств в работе нет — хвалить нечего
    return _READINESS_TONE.get(state, "success")


def _production_readiness_actions(production: dict) -> list[DashboardAction]:
    """По пункту очереди на каждое производство, где материалы в проблемном
    состоянии, — со ссылкой на его карточку."""
    actions = []
    for item in production.get("attention_productions", []):
        reasons = item["reasons"]
        description = reasons[0] if reasons else item["materials_label"]
        extra = item["reasons_count"] - 1
        if extra > 0:
            description += f" (и ещё причин: {extra})"
        actions.append(DashboardAction(
            id=f"production:readiness:{item['production_id']}", section="production",
            title=f"{item['production']}: {item['materials_label'].lower()}",
            description=description, href=f"/production/{item['production_id']}",
            count=item["reasons_count"], tone=_readiness_tone(item["materials_state"]),
        ))
    return actions


def _production_clear_text(production: dict) -> str | None:
    worst = production.get("worst_state")
    if worst is None:
        return "Производств в работе нет."
    if worst in ("provided", "not_required"):
        return ALL_CLEAR_TEXT["production"]
    return None


def generate_today(db: Session, user: User) -> TodayDashboardOut:
    snapshot = build_snapshot(db, user)
    widgets = []
    for section, title, metric, attention in METRICS:
        if section not in snapshot:
            continue
        value = int(snapshot[section][metric])
        if section == "production" and metric == _READINESS_METRIC:
            widgets.append(DashboardWidget(
                section=section, title=title, value=str(value),
                hint=snapshot[section].get("worst_state_label"),
                tone=_readiness_tone(snapshot[section].get("worst_state")),
            ))
            continue
        href = None
        if (section, metric) == ("tasks", "open_tasks"):
            scope = snapshot["tasks"]["open_tasks_scope"]
            title = f"{title} ({'все' if scope == 'all' else 'мои'})"
            href = f"/tasks?scope={scope}&status=open"
        widgets.append(DashboardWidget(
            section=section, title=title, value=str(value),
            tone=("warning" if value else "success") if attention else "neutral",
            href=href,
        ))
    actions = []
    for section, metric, title, description, href, tone in ATTENTION:
        if section not in snapshot:
            continue
        if section == "production" and metric == _READINESS_METRIC:
            actions.extend(_production_readiness_actions(snapshot[section]))
        elif snapshot[section][metric] > 0:
            actions.append(DashboardAction(
                id=f"{section}:{metric}", section=section, title=title,
                description=description, href=href, count=int(snapshot[section][metric]), tone=tone,
            ))
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


# --------------------------------------------------------- по одному разделу --

SECTION_CACHE_TTL = timedelta(hours=6)


def _section_builder(user: User, section: str) -> Callable[[Session], dict] | None:
    """None — раздела нет в METRICS/ATTENTION (например «Совещание»/MAX/«Поставщики»
    сейчас без посчитанного сигнала) или у пользователя нет доступа к нему. Не
    трогает базу — только определяет, есть ли смысл дальше идти в кэш/снепшот."""
    if section == "users":
        return _snapshot_users if user.role == UserRole.ADMIN else None
    entry = SECTION_BUILDERS.get(section)
    if entry is None:
        return None
    module, builder = entry
    return builder if user.has_access(module) else None


def generate_section_signal(db: Session, user: User, section: str, force: bool = False) -> SectionSignalOut:
    """Один раздел «Работы» отдельным запросом — то же действие, что попало бы
    в `TodayDashboardOut.actions`, но не держит остальные плитки, если этот
    раздел тяжело считать, и кэшируется на 6 часов (общий кеш на организацию —
    факт один и тот же для всех, кому раздел вообще доступен; неавторизованный
    запрос до кеша не доходит вовсе, см. `_section_builder` выше). Кэш
    проверяется **до** пересчёта снепшота — попадание в кэш не должно стоить
    того же похода в базу, что и промах."""
    now = datetime.now(timezone.utc)
    builder = _section_builder(user, section)
    if builder is None:
        return SectionSignalOut(section=section, action=None, checked=False, generated_at=now)

    cache_key = f"dashboard_section_signal:{section}"
    if not force:
        cached = ai_cache.get(db, cache_key, ttl=SECTION_CACHE_TTL)
        if cached is not None:
            return SectionSignalOut.model_validate(cached)

    snapshot = builder(db)
    action = None
    for sec, metric, title, description, href, tone in ATTENTION:
        if sec != section:
            continue
        count = int(snapshot.get(metric, 0))
        if count > 0:
            if sec == "production" and metric == _READINESS_METRIC:
                # Один пункт на раздел: худшее состояние и первая причина;
                # единственное проблемное производство — сразу в его карточку.
                items = _production_readiness_actions(snapshot)
                first = items[0]
                action = first if len(items) == 1 else DashboardAction(
                    id=f"{sec}:{metric}", section=sec,
                    title=f"Материалы производства: {snapshot['worst_state_label'].lower()}",
                    description=f"{first.title} — {first.description}", href=href, count=count,
                    tone=_readiness_tone(snapshot.get("worst_state")),
                )
            else:
                action = DashboardAction(
                    id=f"{sec}:{metric}", section=sec, title=title,
                    description=description, href=href, count=count, tone=tone,
                )
            break

    checked = section in ATTENTION_SECTIONS
    clear_text = None
    if checked and action is None:
        clear_text = _production_clear_text(snapshot) if section == "production" else ALL_CLEAR_TEXT.get(section)
    out = SectionSignalOut(section=section, action=action, checked=checked, clear_text=clear_text, generated_at=now)
    ai_cache.set(db, cache_key, out.model_dump(mode="json"), now)
    return out
