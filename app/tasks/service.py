from datetime import datetime, timezone

from fastapi import HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.common.files import FileAsset, FilePurpose, save_upload_file
from app.common.module_access import Module
from app.tasks import sync as task_sync
from app.tasks import timelog
from app.tasks.models import Task, TaskLinkType, TaskReport, TaskReportKind, TaskStatus
from app.users.models import User

ALLOWED_MANUAL_TRANSITIONS = {
    (TaskStatus.READY, TaskStatus.IN_PROGRESS): "assignee",
    (TaskStatus.IN_PROGRESS, TaskStatus.IN_REVIEW): "assignee",
    (TaskStatus.IN_REVIEW, TaskStatus.IN_PROGRESS): "reviewer",
    (TaskStatus.IN_REVIEW, TaskStatus.DONE): "reviewer",
}

# Which access-controlled section a linked task belongs to - see task 0021.
# Tasks not in this map (link_type NONE without a block_id) have no section
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
    """The section this task is bound to, if any (block_id set -> production;
    otherwise looked up from link_type). None means unbound - open to everyone
    with access to `tasks`."""
    if task.block_id is not None:
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


def _validate_responsible(db: Session, responsible_id: int | None) -> None:
    # Ответственный не обязан быть среди assignees (например начальник
    # производства как ответственный за задачу подрядчика-исполнителя) —
    # проверяем только то, что такой пользователь вообще существует.
    if responsible_id is not None and db.get(User, responsible_id) is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Ответственный не найден")


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
    responsible_id: int | None = None,
    depends_on_ids: list[int] = (),
    image_ids: list[int] = (),
    block_id: int | None = None,
    link_type: TaskLinkType = TaskLinkType.NONE,
    link_id: int | None = None,
    link_meta: dict | None = None,
) -> Task:
    _validate_responsible(db, responsible_id)
    depends_on = _resolve_tasks(db, list(depends_on_ids))
    task = Task(
        title=title,
        description=description,
        deadline=deadline,
        block_id=block_id,
        responsible_id=responsible_id,
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
    responsible_id: int | None = None,
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
    if responsible_id is not None:
        _validate_responsible(db, responsible_id)
        task.responsible_id = responsible_id
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


def _add_report(
    db: Session,
    task: Task,
    actor: User,
    *,
    kind: TaskReportKind,
    comment: str,
    files: list[UploadFile],
) -> None:
    report = TaskReport(task_id=task.id, author_id=actor.id, kind=kind, comment=comment)
    report.files = [
        save_upload_file(db, upload, FilePurpose.TASK_REPORT_FILE, actor)
        for upload in files
        if upload is not None and upload.filename
    ]
    db.add(report)
    db.flush()


def submit_report(
    db: Session,
    task: Task,
    actor: User,
    *,
    comment: str,
    files: list[UploadFile] = (),
) -> Task:
    """Сдать задачу с отчётом: исполнитель пишет, что сделано, и при
    необходимости прикладывает файлы. Отчёт и перевод in_progress -> in_review
    происходят вместе — при любой ошибке (не тот статус, не исполнитель,
    пустой комментарий) не сохраняется ни то, ни другое.

    Задача без проверяющих после этого сразу становится DONE (обычное
    поведение _finalize_status), отчёт при этом остаётся у задачи.
    """
    text = (comment or "").strip()
    if not text:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Напишите комментарий о выполненной задаче",
        )
    if task.status != TaskStatus.IN_PROGRESS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Сдать можно только задачу в работе",
        )
    if actor not in task.assignees:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Сдать задачу может только исполнитель",
        )

    _add_report(
        db, task, actor,
        kind=TaskReportKind.SUBMISSION, comment=text, files=list(files),
    )
    return _finalize_status(db, task, TaskStatus.IN_REVIEW, actor=actor)


def review_task(
    db: Session,
    task: Task,
    actor: User,
    *,
    accept: bool,
    comment: str = "",
    files: list[UploadFile] = (),
) -> Task:
    """Решение проверяющего с отчётом: принять задачу (in_review -> done) или
    вернуть в работу (in_review -> in_progress), приложив комментарий и файлы.

    В отличие от сдачи исполнителем, комментарий и файлы необязательны: если
    не приложено ничего, запись в журнал отчётов не добавляется — просто
    меняется статус. Проверки статуса и роли — те же, что у обычного перехода
    (см. set_status), поэтому решение идёт через него.
    """
    attachments = [f for f in files if f is not None and f.filename]
    text = (comment or "").strip()
    target = TaskStatus.DONE if accept else TaskStatus.IN_PROGRESS

    if task.status != TaskStatus.IN_REVIEW:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Принять или вернуть можно только задачу на проверке",
        )
    if actor not in task.reviewers:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Перевести задачу может только проверяющий",
        )

    if text or attachments:
        _add_report(
            db, task, actor,
            kind=TaskReportKind.REVIEW_ACCEPTED if accept else TaskReportKind.REVIEW_RETURNED,
            comment=text,
            files=attachments,
        )

    return set_status(db, task, target, actor)


def edit_report_comment(db: Session, task: Task, report_id: int, actor: User, comment: str) -> TaskReport:
    """Поправить текст своей записи журнала — в любой момент, в том числе
    после того, как задачу приняли: отчёт часто дополняют по итогам разговора,
    и закрытая задача не должна этому мешать.

    Менять можно только свой комментарий и только текст: вид записи, автора,
    дату отправки и вложения правка не трогает.
    """
    report = db.get(TaskReport, report_id)
    if report is None or report.task_id != task.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Отчёт не найден")
    if report.author_id != actor.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Изменить комментарий может только его автор",
        )

    text = (comment or "").strip()
    if not text and not report.files:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Комментарий нельзя оставить пустым",
        )

    if text != report.comment:
        report.comment = text
        report.updated_at = datetime.now(timezone.utc)
        db.flush()
    return report


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
