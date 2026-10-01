"""Политика приёмки задач (0084-f): какие задачи можно закрыть сдачей без
проверки, а какие — только после приёмки другим человеком.

Единственное место решения. Поменять список «требует приёмки» = поправить
`REVIEW_POLICY` и тест в tests/test_task_review_policy.py.

Полей «риск» / «тип» у задачи нет, поэтому вид задачи определяется по тому,
что уже есть в модели: `block_id` — задача блока производства.
"""

import enum

from app.tasks.models import Task
from app.users.models import User


class TaskKind(str, enum.Enum):
    # Задача блока производства (Task.block_id не пустой).
    PRODUCTION_BLOCK = "production_block"
    # Всё остальное: ручные задачи, задачи по клиенту, складу, маркетингу.
    OTHER = "other"


class ReviewPolicy(str, enum.Enum):
    # Сдача без проверяющего не закрывает задачу: она ждёт приёмки.
    REVIEW_REQUIRED = "review_required"
    # Сдача без проверяющего сразу закрывает задачу (поведение до 0084-f).
    AUTO_CLOSE_ALLOWED = "auto_close_allowed"


REVIEW_POLICY: dict[TaskKind, ReviewPolicy] = {
    TaskKind.PRODUCTION_BLOCK: ReviewPolicy.REVIEW_REQUIRED,
    TaskKind.OTHER: ReviewPolicy.AUTO_CLOSE_ALLOWED,
}


def task_kind(task: Task) -> TaskKind:
    if task.block_id is not None:
        return TaskKind.PRODUCTION_BLOCK
    return TaskKind.OTHER


def review_policy(task: Task) -> ReviewPolicy:
    return REVIEW_POLICY[task_kind(task)]


def requires_review(task: Task) -> bool:
    return review_policy(task) == ReviewPolicy.REVIEW_REQUIRED


def can_accept_own_work(task: Task, user: User) -> bool:
    """Может ли `user` принять задачу, в которой он сам исполнитель. Для
    задач, требующих приёмки, — нет: сдал и сам же принял — это не приёмка."""
    return not (requires_review(task) and user in task.assignees)


def eligible_reviewers(task: Task) -> list[User]:
    """Проверяющие, которые реально могут принять задачу. Для задач,
    требующих приёмки, исполнители из этого списка исключаются."""
    return [u for u in task.reviewers if can_accept_own_work(task, u)]
