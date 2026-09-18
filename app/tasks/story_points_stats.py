"""Подсчёт сторипоинтов по сотруднику (0070-d) - внутренние строительные
блоки без собственного HTTP-роута; используются `0070-e` (план на день) и
`0070-f` (виджеты загруженности). story_points IS NULL считается как 0
(задача ещё не оценена ИИ или создана до backfill).
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.tasks.models import Task, TaskStatus, TaskWorkDuration, task_assignees
from app.tasks.schemas import WorkloadLevel, WorkloadOut
from app.users.models import User

# Пороги общей загруженности по открытым очкам - подобраны по умолчанию,
# см. 0070-f; переставить, если реальные данные компании покажут, что
# границы сильно не совпадают с ощущением команды.
WORKLOAD_LOW_MAX = 5
WORKLOAD_MEDIUM_MAX = 12


def completed_points_by_user(db: Session, since: datetime) -> dict[int, int]:
    """Сумма story_points по задачам каждого исполнителя, завершённым
    (status == DONE) с моментом завершения (TaskWorkDuration.completed_at)
    не раньше `since`."""
    tasks = (
        db.query(Task)
        .join(TaskWorkDuration, TaskWorkDuration.task_id == Task.id)
        .filter(Task.status == TaskStatus.DONE, TaskWorkDuration.completed_at >= since)
        .all()
    )
    result: dict[int, int] = {}
    for task in tasks:
        points = task.story_points or 0
        for user in task.assignees:
            result[user.id] = result.get(user.id, 0) + points
    return result


def open_points_by_user(db: Session) -> dict[int, int]:
    """Сумма story_points по открытым (status != DONE) задачам каждого
    исполнителя - для оценки текущей загрузки."""
    tasks = db.query(Task).filter(Task.status != TaskStatus.DONE).all()
    result: dict[int, int] = {}
    for task in tasks:
        points = task.story_points or 0
        for user in task.assignees:
            result[user.id] = result.get(user.id, 0) + points
    return result


def _workload_level(open_story_points: int) -> WorkloadLevel:
    if open_story_points <= WORKLOAD_LOW_MAX:
        return WorkloadLevel.LOW
    if open_story_points <= WORKLOAD_MEDIUM_MAX:
        return WorkloadLevel.MEDIUM
    return WorkloadLevel.HIGH


def workload_by_user(db: Session, *, now: datetime | None = None) -> list[WorkloadOut]:
    """Агрегат для вкладки «Сотрудники» раздела «Задачи» (0070-f, только
    администратор - в отличие от остального интерфейса задач, сторипоинты
    здесь показываются явно). Только сотрудники, которые были исполнителем
    хотя бы одной задачи когда-либо (открытой или выполненной)."""
    now = now or datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)
    two_weeks_ago = now - timedelta(days=14)

    ever_assignee_ids = {row[0] for row in db.query(task_assignees.c.user_id).distinct().all()}
    if not ever_assignee_ids:
        return []

    open_tasks_count: dict[int, int] = {}
    overdue_count: dict[int, int] = {}
    for task in db.query(Task).filter(Task.status != TaskStatus.DONE).all():
        for user in task.assignees:
            open_tasks_count[user.id] = open_tasks_count.get(user.id, 0) + 1
            if task.deadline and task.deadline < now:
                overdue_count[user.id] = overdue_count.get(user.id, 0) + 1

    open_points = open_points_by_user(db)
    # completed_last_14 покрывает оба 7-дневных окна разом - разница даёт
    # именно предыдущую неделю (14..7 дней назад), не всё время до неё.
    completed_last_14 = completed_points_by_user(db, two_weeks_ago)
    completed_last_7 = completed_points_by_user(db, week_ago)

    users_by_id = {u.id: u for u in db.query(User).filter(User.id.in_(ever_assignee_ids)).all()}

    result = []
    for user_id in ever_assignee_ids:
        user = users_by_id.get(user_id)
        if user is None:
            continue
        open_story_points = open_points.get(user_id, 0)
        completed_7d = completed_last_7.get(user_id, 0)
        completed_prev_7d = completed_last_14.get(user_id, 0) - completed_7d
        result.append(
            WorkloadOut(
                user_id=user_id,
                full_name=user.full_name,
                open_tasks_count=open_tasks_count.get(user_id, 0),
                open_story_points=open_story_points,
                completed_points_7d=completed_7d,
                completed_points_prev_7d=completed_prev_7d,
                overdue_count=overdue_count.get(user_id, 0),
                workload_level=_workload_level(open_story_points),
            )
        )
    result.sort(key=lambda r: r.full_name)
    return result


def average_open_points(db: Session, *, exclude_user_id: int | None = None) -> float:
    """Средняя текущая загрузка (открытые очки) по сотрудникам с хотя бы
    одной открытой задачей - используется `0070-e` как приватный сигнал
    «загруженность других» (только число, без имён и задач) в промпте плана
    на день. `exclude_user_id` убирает самого сотрудника, для которого
    строится план, чтобы сравнение было именно с «остальными»."""
    points_by_user = open_points_by_user(db)
    if exclude_user_id is not None:
        points_by_user.pop(exclude_user_id, None)
    if not points_by_user:
        return 0.0
    return sum(points_by_user.values()) / len(points_by_user)
