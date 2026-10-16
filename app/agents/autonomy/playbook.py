"""Плейбук автономного агента (0112): детекторы сценариев по реальным данным.

Детектор только находит отклонение и предлагает уровень — ничего не пишет. Уровень
потом проходит через guardrails (ограничители) и счётчики повторов. Реализованы сценарии,
для которых в системе есть данные; остальные из задачи пока не подключены.

  task_stalled      №2   задача давно не взята в работу
  no_supplies       №6   нет поставок на склад N дней
  client_completed  №13  клиент дошёл до «Завершён»
  idle_wide_access  №18  неактивный сотрудник с широким доступом
  max_digest        —    сводка для Арсения Щекина в MAX (только предложение, отправка после согласования)
  cycle_stuck       №9*  цикл клиента завис 14+ дней (упрощённо, без привязки к цели совета)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.agents.autonomy.workspace import aware, utcnow
from app.clients.models import Client, ClientStage
from app.common.module_access import AccessLevel, Module
from app.core.config import settings
from app.cycle.models import Cycle, CycleStatus
from app.tasks.models import Task, TaskLinkType, TaskStageEvent, TaskStatus
from app.users.models import User, UserRole
from app.warehouse.models import Supply, WarehouseMaterial

STALLED_AFTER = timedelta(days=3)
OVERLOAD_OPEN_TASKS = 8
NO_SUPPLIES_DAYS = 14
IDLE_DAYS = 30
WIDE_ACCESS_MODULES = 3
CYCLE_STUCK_DAYS = 14
COMPLETED_WINDOW = timedelta(days=7)
MAX_PER_SCENARIO = 10

SCENARIO_SECTION = {
    "task_stalled": "tasks",
    "no_supplies": "warehouse",
    "client_completed": "clients",
    "idle_wide_access": "users",
    "cycle_stuck": "cycle",
    "max_digest": "tasks",
}


@dataclass
class Finding:
    scenario: str
    key: str
    section: str
    # Предложенный детектором уровень. Итоговый определяют guardrails.
    level: str
    title: str
    text: str
    recipients: list[int] = field(default_factory=list)
    href: str = "/"
    module: Module = Module.TASKS
    # Для наблюдения: через сколько эскалировать, если не сдвинулось.
    escalate_in: timedelta | None = None
    # Действие потребует внешней отправки (клиенту и т.п.) — тогда только Б.
    external: bool = False
    # Данные противоречивы — вместо действия карточка с вопросом.
    uncertain: bool = False
    # Для внешнего сообщения в MAX: {"chat_id", "recipient", "text"}. Только предложение.
    max_message: dict | None = None


def _admin_ids(db: Session) -> list[int]:
    return [u.id for u in db.query(User).filter(User.role == UserRole.ADMIN, User.is_active.is_(True))]


def _module_user_ids(db: Session, module: Module) -> list[int]:
    ids = []
    for user in db.query(User).filter(User.role == UserRole.WORKER, User.is_active.is_(True)):
        if user.access_level(module) >= AccessLevel.EDIT:
            ids.append(user.id)
    return ids


def task_stalled(db: Session) -> list[Finding]:
    now = utcnow()
    tasks = (
        db.query(Task)
        .filter(Task.status == TaskStatus.READY, Task.created_at < now - STALLED_AFTER)
        .order_by(Task.created_at)
        .limit(MAX_PER_SCENARIO * 3)
        .all()
    )
    admins = _admin_ids(db)
    out: list[Finding] = []
    for task in tasks:
        if not task.assignees:
            continue
        # Не работаем с карточками самого агента.
        if task.link_type.value == "agent_proposal":
            continue
        age_days = (now - aware(task.created_at)).days
        loads = {
            user.id: db.query(Task)
            .filter(Task.assignees.any(User.id == user.id), Task.status.in_([TaskStatus.IN_PROGRESS, TaskStatus.READY]))
            .count()
            for user in task.assignees
        }
        overloaded = [u for u in task.assignees if loads[u.id] >= OVERLOAD_OPEN_TASKS]
        names = ", ".join(u.full_name for u in task.assignees)
        base = f"Задача «{task.title}» (№{task.id}) не взята в работу {age_days} дн."
        if overloaded:
            who = ", ".join(f"{u.full_name} — {loads[u.id]} открытых" for u in overloaded)
            out.append(Finding(
                scenario="task_stalled", key=f"task_stalled:{task.id}", section="tasks", level="B",
                title=f"Перенести задачу «{task.title}»?",
                text=f"{base} Исполнитель перегружен ({who}). Предлагаю передать задачу тому, у кого меньше нагрузки.",
                recipients=admins, href="/tasks", module=Module.TASKS,
            ))
        else:
            out.append(Finding(
                scenario="task_stalled", key=f"task_stalled:{task.id}", section="tasks", level="A",
                title=f"Напоминание: задача «{task.title}»",
                text=f"{base} Исполнитель: {names}. Нагрузка в норме — напоминаю взять в работу.",
                recipients=[u.id for u in task.assignees], href="/tasks", module=Module.TASKS,
                escalate_in=timedelta(days=3),
            ))
        if len(out) >= MAX_PER_SCENARIO:
            break
    return out


def no_supplies(db: Session) -> list[Finding]:
    if db.query(func.count(WarehouseMaterial.id)).scalar() == 0:
        return []
    since = utcnow() - timedelta(days=NO_SUPPLIES_DAYS)
    if db.query(Supply).filter(Supply.created_at >= since).count() > 0:
        return []
    recipients = sorted(set(_module_user_ids(db, Module.WAREHOUSE) + _admin_ids(db)))
    return [Finding(
        scenario="no_supplies", key="no_supplies", section="warehouse", level="A",
        title="Нет поставок на склад",
        text=f"За {NO_SUPPLIES_DAYS} дней на склад не оформлено ни одной поставки. "
             "Сверьте заявки поставщикам и приход на складе.",
        recipients=recipients, href="/warehouse", module=Module.WAREHOUSE,
        escalate_in=timedelta(days=3),
    )]


def client_completed(db: Session) -> list[Finding]:
    since = utcnow() - COMPLETED_WINDOW
    # У клиента нет отметки «когда завершён», поэтому берём только тех, кто сейчас
    # на «Завершён» и заведён не раньше окна смотрения — иначе подняли бы всю историю.
    clients = (
        db.query(Client)
        .filter(Client.stage == ClientStage.COMPLETED, Client.created_at >= since)
        .limit(MAX_PER_SCENARIO).all()
    )
    admins = _admin_ids(db)
    out = []
    for client in clients:
        who = [client.manager_id] if client.manager_id else admins
        out.append(Finding(
            scenario="client_completed", key=f"client_completed:{client.id}", section="clients", level="A",
            title=f"Клиент «{client.full_name}» завершён — опрос и отзыв",
            text=f"Клиент «{client.full_name}» дошёл до «Завершён». Опрос удовлетворённости клиенту "
                 "агент сам не отправляет (внешнее сообщение): подготовьте опрос и после хорошего ответа попросите отзыв.",
            recipients=[w for w in who if w], href="/clients", module=Module.CLIENTS,
            external=True,
        ))
    return out


def idle_wide_access(db: Session) -> list[Finding]:
    now = utcnow()
    cutoff = now - timedelta(days=IDLE_DAYS)
    out = []
    admins = _admin_ids(db)
    for user in db.query(User).filter(User.role == UserRole.WORKER, User.is_active.is_(True)):
        if aware(user.created_at) > cutoff:
            continue
        wide = [g for g in user.module_access if g.level >= AccessLevel.EDIT]
        if len(wide) < WIDE_ACCESS_MODULES:
            continue
        # Единственный источник активности в системе — переходы статусов задач.
        # Нет ни одного перехода за IDLE_DAYS — «неактивен по задачам»; это
        # приближение, поэтому в тексте так и сказано, а решение — за админом.
        acted = db.query(TaskStageEvent).filter(
            TaskStageEvent.actor_id == user.id, TaskStageEvent.created_at >= cutoff
        ).count()
        if acted:
            continue
        out.append(Finding(
            scenario="idle_wide_access", key=f"idle_wide_access:{user.id}", section="users", level="B",
            title=f"Сузить доступ: {user.full_name}?",
            text=f"У {user.full_name} права на правку в {len(wide)} разделах, а действий по задачам за "
                 f"{IDLE_DAYS} дней нет (других данных об активности в системе нет). "
                 "Предлагаю сузить доступ до просмотра — решение за администратором, агент доступ не меняет.",
            recipients=admins, href="/admin", module=Module.TASKS,
        ))
        if len(out) >= MAX_PER_SCENARIO:
            break
    return out


def cycle_stuck(db: Session) -> list[Finding]:
    threshold = utcnow() - timedelta(days=CYCLE_STUCK_DAYS)
    rows = (
        db.query(Cycle.id, Client.id, Client.full_name, Client.manager_id)
        .join(Client, Client.cycle_id == Cycle.id)
        .filter(Cycle.status == CycleStatus.CLIENT, Client.created_at < threshold,
                Client.stage != ClientStage.COMPLETED)
        .limit(MAX_PER_SCENARIO).all()
    )
    admins = _admin_ids(db)
    out = []
    for cycle_id, client_id, name, manager_id in rows:
        out.append(Finding(
            scenario="cycle_stuck", key=f"cycle_stuck:{cycle_id}", section="cycle", level="A",
            title=f"Цикл клиента «{name}» завис",
            text=f"Цикл клиента «{name}» остаётся на стадии клиента больше {CYCLE_STUCK_DAYS} дней. "
                 "Выясните, что мешает, и зафиксируйте следующий шаг.",
            recipients=[manager_id] if manager_id else admins, href="/cycles", module=Module.CYCLE,
            escalate_in=timedelta(days=3),
        ))
    return out


def max_digest(db: Session) -> list[Finding]:
    """Раз в сутки предлагает короткую сводку для получателя в MAX. Не отправляет:
    это карточка «на согласование», уйдёт только после решения администратора."""
    if not settings.agent_max_chat_id:
        return []
    now = utcnow()
    stalled = db.query(Task).filter(
        Task.status == TaskStatus.READY, Task.created_at < now - STALLED_AFTER,
        Task.link_type != TaskLinkType.AGENT_PROPOSAL,
    ).count()
    proposals = sum(
        1 for t in db.query(Task).filter(
            Task.link_type == TaskLinkType.AGENT_PROPOSAL, Task.status == TaskStatus.IN_REVIEW
        )
        if (t.link_meta or {}).get("scenario") != "max_send"  # сами сообщения в сводку не считаем
    )
    if not stalled and not proposals:
        return []
    lines = []
    if stalled:
        lines.append(f"• задач не взято в работу дольше {STALLED_AFTER.days} дн.: {stalled}")
    if proposals:
        lines.append(f"• предложений агента ждут решения: {proposals}")
    recipient = settings.agent_max_recipient
    text = f"{recipient}, сводка Durov OS на {now.strftime('%d.%m.%Y')}:\n" + "\n".join(lines)
    return [Finding(
        scenario="max_digest", key=f"max_digest:{now.strftime('%Y-%m-%d')}", section="tasks", level="B",
        title=f"Сводка в MAX: {recipient}",
        text=f"Предлагаю отправить сводку в MAX. {text}",
        recipients=_admin_ids(db), href="/tasks", module=Module.TASKS, external=True,
        max_message={"chat_id": settings.agent_max_chat_id, "recipient": recipient, "text": text},
    )]


DETECTORS = {
    "tasks": [task_stalled, max_digest],
    "warehouse": [no_supplies],
    "clients": [client_completed],
    "users": [idle_wide_access],
    "cycle": [cycle_stuck],
}
