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
