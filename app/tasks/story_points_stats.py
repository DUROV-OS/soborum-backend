"""Подсчёт сторипоинтов по сотруднику (0070-d) - внутренние строительные
блоки без собственного HTTP-роута; используются `0070-e` (план на день) и
`0070-f` (виджеты загруженности). story_points IS NULL считается как 0
(задача ещё не оценена ИИ или создана до backfill).
"""

from datetime import datetime

from sqlalchemy.orm import Session

from app.tasks.models import Task, TaskStatus, TaskWorkDuration


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
