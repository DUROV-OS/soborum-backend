"""Плановая проверка сроков задач (0080-c): `TASK_DUE` / `TASK_OVERDUE`.

Срок задачи (`Task.deadline`) не меняется сам по себе — в отличие от
событийных хуков в сервисах разделов (клиент, производство, монтаж, проводки,
контент, заявки «Пожелания»), здесь нет момента в коде, который можно
перехватить вызовом. Нужен периодический обход всех открытых задач со сроком.

Выбор механизма: в проекте уже есть прецедент простого фонового цикла без
новых зависимостей — `app.tasks.story_points_backfill.start_story_points_loop`
(тонкий daemon-поток, интервал из настроек, старт из `app.main`). Это ровно
тот же паттерн: без APScheduler (он не используется в проекте — см.
requirements) выделять отдельный процесс/cron под одну функцию раз в сутки
избыточно.

Дедуп (TASK_DUE/TASK_OVERDUE ровно один раз на задачу и порог — не только
между перезапусками процесса, но и между последовательными вызовами проверки
для одной и той же задачи) — `TaskDeadlineAlert` (app.jobs.models):
отдельная запись-отметка «по этой задаче и порогу уведомление уже отправлено
(или получателя не было)», её и проверяем перед тем, как звать `notify`.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.common.module_access import Module
from app.core.config import settings
from app.db.session import SessionLocal
from app.jobs.models import DeadlineThreshold, TaskDeadlineAlert
from app.notifications.models import NotificationKind
from app.notifications.service import notify
from app.tasks.models import Task, TaskStatus

log = logging.getLogger("app.jobs.deadlines")
_started = False

# «Скоро дедлайн» — срок в пределах этого окна от текущего момента.
DUE_SOON_WINDOW_SECONDS = 24 * 3600


def _already_alerted(db: Session, task_id: int, threshold: DeadlineThreshold) -> bool:
    return (
        db.query(TaskDeadlineAlert)
        .filter(TaskDeadlineAlert.task_id == task_id, TaskDeadlineAlert.threshold == threshold)
        .first()
        is not None
    )


def _alert(db: Session, task: Task, threshold: DeadlineThreshold, kind: NotificationKind, title: str) -> bool:
    """Создаёт уведомление и отметку о пороге, если их ещё не было для этой
    пары задача/порог. Возвращает True, если уведомление реально создано
    (отметка при этом ставится в любом случае — даже без получателя)."""
    if _already_alerted(db, task.id, threshold):
        return False
    db.add(TaskDeadlineAlert(task_id=task.id, threshold=threshold))
    if task.responsible_id is None:
        db.commit()
        return False
    notify(
        db,
        task.responsible_id,
        kind=kind,
        module=Module.TASKS,
        title=title,
        object_type="task",
        object_id=task.id,
    )
    db.commit()
    return True


def check_task_deadlines(db: Session, *, now: datetime | None = None) -> dict:
    """Один прогон плановой проверки. Пропускает задачи без срока и
    закрытые (`TaskStatus.DONE`) — им уведомление по сроку не нужно.
    Не коммитит ничего, кроме уже закоммиченного внутри `_alert` построчно —
    безопасно звать повторно."""
    now = now or datetime.now(timezone.utc)
    due_soon = 0
    overdue = 0
    tasks = db.query(Task).filter(Task.deadline.isnot(None), Task.status != TaskStatus.DONE).all()
    for task in tasks:
        deadline = task.deadline
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        if deadline < now:
            if _alert(
                db,
                task,
                DeadlineThreshold.OVERDUE,
                NotificationKind.TASK_OVERDUE,
                title=f"Просрочена задача «{task.title}»",
            ):
                overdue += 1
        elif (deadline - now).total_seconds() <= DUE_SOON_WINDOW_SECONDS:
            if _alert(
                db,
                task,
                DeadlineThreshold.DUE_SOON,
                NotificationKind.TASK_DUE,
                title=f"Скоро дедлайн задачи «{task.title}»",
            ):
                due_soon += 1
    return {"checked": len(tasks), "due_soon": due_soon, "overdue": overdue}


def start_task_deadline_loop() -> None:
    global _started
    if _started or not settings.task_deadline_check_autorun:
        return
    _started = True
    thread = threading.Thread(target=_run, name="task-deadlines", daemon=True)
    thread.start()
    log.info("Проверка сроков задач: каждые %s с", settings.task_deadline_check_interval_seconds)


def _run() -> None:
    time.sleep(30)
    while True:
        db = SessionLocal()
        try:
            result = check_task_deadlines(db)
            if result["due_soon"] or result["overdue"]:
                log.info("Сроки задач: %s", result)
        except Exception:
            db.rollback()
            log.exception("Проверка сроков задач не прошла")
        finally:
            db.close()
        time.sleep(max(60, settings.task_deadline_check_interval_seconds))
