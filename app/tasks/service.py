from datetime import datetime

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.common.files import FileAsset
from app.common.module_access import Module
from app.tasks import sync as task_sync
from app.tasks import timelog
from app.tasks.models import Task, TaskLinkType, TaskStatus
from app.users.models import User

ALLOWED_MANUAL_TRANSITIONS = {
    (TaskStatus.READY, TaskStatus.IN_PROGRESS): "assignee",
    (TaskStatus.IN_PROGRESS, TaskStatus.IN_REVIEW): "assignee",
    (TaskStatus.IN_REVIEW, TaskStatus.IN_PROGRESS): "reviewer",
    (TaskStatus.IN_REVIEW, TaskStatus.DONE): "reviewer",
}

# Which access-controlled section a linked task belongs to - see task 0021.
# Tasks not in this map (link_type NONE without a module_id) have no section
# restriction: they are visible/claimable by anyone with access to `tasks`.
_LINK_TYPE_SECTION: dict[TaskLinkType, Module] = {
    TaskLinkType.CLIENT_STAGE: Module.CLIENTS,
    TaskLinkType.CLIENT_BALANCE_PAYMENT: Module.CLIENTS,
    TaskLinkType.CONTENT_STAGE: Module.MARKETING,
    TaskLinkType.WAREHOUSE_REQUEST: Module.WAREHOUSE,
    TaskLinkType.WAREHOUSE_SHORTAGE: Module.WAREHOUSE,
    TaskLinkType.SUPPLIER_PRICE_BACKFILL: Module.WAREHOUSE,
    TaskLinkType.MONEY_MOVEMENT_APPROVAL: Module.ACCOUNTING,
    TaskLinkType.MONEY_MOVEMENT_BACKFILL: Module.ACCOUNTING,
}


def task_section(task: Task) -> Module | None:
    """The section this task is bound to, if any (module_id set -> production;
    otherwise looked up from link_type). None means unbound - open to everyone
    with access to `tasks`."""
    if task.module_id is not None:
        return Module.PRODUCTION
    return _LINK_TYPE_SECTION.get(task.link_type)


def is_claimable_for(task: Task, user: User) -> bool:
    """A "свободная задача": ready, no assignees yet, and either unbound or in
    a section this user has access to."""
    if task.status != TaskStatus.READY or task.assignees:
        return False
    section = task_section(task)
    return section is None or user.has_access(section)


def is_mine_or_claimable(task: Task, user: User) -> bool:
    if user in task.assignees or user in task.reviewers:
        return True
    return is_claimable_for(task, user)


def claim_task(db: Session, task: Task, user: User) -> Task:
    if task.status != TaskStatus.READY or task.assignees:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Задачу нельзя взять: она уже не свободна",
        )
    section = task_section(task)
    if section is not None and not user.has_access(section):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Нет доступа к разделу «{section.value}»",
        )
    task.assignees = [user]
    db.flush()
    return task


def get_task_or_404(db: Session, task_id: int) -> Task:
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Задача не найдена")
    return task


def _resolve_users(db: Session, ids: list[int]) -> list[User]:
    if not ids:
        return []
    users = db.query(User).filter(User.id.in_(ids)).all()
    if len(users) != len(set(ids)):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Один или несколько пользователей не найдены")
    return users


def _resolve_tasks(db: Session, ids: list[int]) -> list[Task]:
    if not ids:
        return []
    tasks = db.query(Task).filter(Task.id.in_(ids)).all()
    if len(tasks) != len(set(ids)):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Одна или несколько зависимых задач не найдены")
    return tasks


def _initial_status(depends_on: list[Task]) -> TaskStatus:
    if not depends_on:
        return TaskStatus.READY
    if all(t.status == TaskStatus.DONE for t in depends_on):
        return TaskStatus.READY
    return TaskStatus.NOT_READY


def create_task(
    db: Session,
    *,
    title: str,
    description: str | None = None,
    deadline: datetime | None = None,
    assignee_ids: list[int] = (),
    reviewer_ids: list[int] = (),
    depends_on_ids: list[int] = (),
    image_ids: list[int] = (),
    module_id: int | None = None,
    link_type: TaskLinkType = TaskLinkType.NONE,
    link_id: int | None = None,
    link_meta: dict | None = None,
) -> Task:
    depends_on = _resolve_tasks(db, list(depends_on_ids))
    task = Task(
        title=title,
        description=description,
        deadline=deadline,
        module_id=module_id,
        link_type=link_type,
        link_id=link_id,
        link_meta=link_meta,
        status=_initial_status(depends_on),
    )
    task.assignees = _resolve_users(db, list(assignee_ids))
    task.reviewers = _resolve_users(db, list(reviewer_ids))
    task.depends_on = depends_on
    if image_ids:
        task.images = db.query(FileAsset).filter(FileAsset.id.in_(list(image_ids))).all()
    db.add(task)
    db.flush()
    timelog.record_stage(db, task, None, task.status, automatic=True, note="created")
    return task


def update_task(
    db: Session,
    task: Task,
    *,
    title: str | None,
    description: str | None,
    deadline: datetime | None,
    assignee_ids: list[int] | None,
    reviewer_ids: list[int] | None,
    depends_on_ids: list[int] | None,
    image_ids: list[int] | None,
) -> Task:
    if title is not None:
        task.title = title
    if description is not None:
        task.description = description
    if deadline is not None:
        task.deadline = deadline
    if assignee_ids is not None:
        task.assignees = _resolve_users(db, assignee_ids)
    if reviewer_ids is not None:
        task.reviewers = _resolve_users(db, reviewer_ids)
    if depends_on_ids is not None:
        task.depends_on = _resolve_tasks(db, depends_on_ids)
        if task.status == TaskStatus.NOT_READY:
            new_status = _initial_status(task.depends_on)
            if new_status != task.status:
                task.status = new_status
                db.flush()
                timelog.record_stage(
                    db, task, TaskStatus.NOT_READY, new_status,
                    automatic=True, note="dependencies changed",
                )
    if image_ids is not None:
        task.images = db.query(FileAsset).filter(FileAsset.id.in_(image_ids)).all()
    db.flush()
    return task


def _cascade_readiness(db: Session, completed_task: Task) -> None:
    dependents = [t for t in completed_task.blocks if t.status == TaskStatus.NOT_READY]
    became_ready = []
    for dependent in dependents:
        if all(dep.status == TaskStatus.DONE for dep in dependent.depends_on):
            dependent.status = TaskStatus.READY
            became_ready.append(dependent)
    db.flush()
    for dependent in became_ready:
        timelog.record_stage(
            db, dependent, TaskStatus.NOT_READY, TaskStatus.READY,
            automatic=True, note="dependencies satisfied",
        )


def _finalize_status(
    db: Session,
    task: Task,
    new_status: TaskStatus,
    *,
    actor: User | None = None,
    automatic: bool = False,
) -> Task:
    """Apply a status that has already been permission-checked (or needs no
    check, e.g. the auto DONE below), then run the readiness cascade and
    task_sync. Not exported: always go through set_status or force_close."""
    previous = task.status
    task.status = new_status
    db.flush()

    if previous != new_status:
        timelog.record_stage(db, task, previous, new_status, actor=actor, automatic=automatic)

    if task.status == TaskStatus.IN_REVIEW and not task.reviewers:
        # Auto-close when there is nobody to review: not attributable to a person.
        return _finalize_status(db, task, TaskStatus.DONE, automatic=True)

    if task.status == TaskStatus.DONE:
        _cascade_readiness(db, task)
        task_sync.handle_task_closed(db, task)

    return task


def set_status(db: Session, task: Task, new_status: TaskStatus, actor: User) -> Task:
    if task.status == new_status:
        return task

    if new_status in (TaskStatus.NOT_READY, TaskStatus.READY):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Статусы «не готова к работе» и «готова к работе» выставляются автоматически",
        )

    role_required = ALLOWED_MANUAL_TRANSITIONS.get((task.status, new_status))
    if role_required is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Недопустимый переход статуса задачи")

    if role_required == "assignee" and actor not in task.assignees:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Перевести задачу может только исполнитель")
    if role_required == "reviewer" and actor not in task.reviewers:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Перевести задачу может только проверяющий")

    return _finalize_status(db, task, new_status, actor=actor)


def force_close(db: Session, task: Task) -> None:
    """Close a task as a side-effect of a domain stage transition.

    Bypasses the normal actor/role checks and does NOT invoke task_sync,
    since this close is itself the result of a domain transition (avoids
    calling back into the transition that triggered it).
    """
    if task.status == TaskStatus.DONE:
        return
    previous = task.status
    task.status = TaskStatus.DONE
    db.flush()
    timelog.record_stage(
        db, task, previous, TaskStatus.DONE, automatic=True, note="closed by domain stage",
    )
    _cascade_readiness(db, task)


def close_open_link_task(db: Session, link_type: TaskLinkType, link_id: int) -> None:
    open_task = (
        db.query(Task)
        .filter(Task.link_type == link_type, Task.link_id == link_id, Task.status != TaskStatus.DONE)
        .first()
    )
    if open_task:
        force_close(db, open_task)


def create_link_task(
    db: Session,
    *,
    title: str,
    link_type: TaskLinkType,
    link_id: int,
    assignees: list[User],
    link_meta: dict | None = None,
) -> Task:
    task = Task(
        title=title,
        status=TaskStatus.READY,
        link_type=link_type,
        link_id=link_id,
        link_meta=link_meta,
    )
    task.assignees = assignees
    db.add(task)
    db.flush()
    timelog.record_stage(db, task, None, task.status, automatic=True, note="created (linked)")
    return task
