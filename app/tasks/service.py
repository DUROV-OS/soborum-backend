from datetime import datetime, timezone

from fastapi import HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.common.files import FileAsset, FilePurpose, save_upload_file
from app.common.module_access import Module
from app.production import readiness
from app.tasks import policy as review_policy
from app.tasks import sync as task_sync
from app.tasks import timelog
from app.tasks.models import Task, TaskLinkType, TaskPriority, TaskReport, TaskReportKind, TaskReportRevision, TaskStatus
from app.tasks.schemas import TaskScope
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


def default_board_scope(user: User) -> TaskScope:
    """Область, в которой пользователь видит «все свои» задачи на доске: «Все
    задачи», если есть доступ `tasks_all`, иначе «Мои задачи» (0084-h)."""
    return TaskScope.ALL if user.has_access(Module.TASKS_ALL) else TaskScope.MINE


def apply_scope(tasks: list[Task], user: User, scope: TaskScope) -> list[Task]:
    """Фильтр `scope` доски (0021): ALL — без фильтра, MINE — свои и свободные,
    CLAIMABLE — только свободные. Права на ALL проверяет вызывающий."""
    if scope == TaskScope.MINE:
        return [t for t in tasks if is_mine_or_claimable(t, user)]
    if scope == TaskScope.CLAIMABLE:
        return [t for t in tasks if is_claimable_for(t, user)]
    return tasks


def open_tasks_query(db: Session, user: User, scope: TaskScope, query=None) -> list[Task]:
    """Открытые (не done) задачи в области `scope` — один набор и для числа на
    Пульсе, и для `GET /api/tasks?status=open` (0084-h). `query` — уже
    отфильтрованный запрос доски (исполнитель, блок и т.д.), по умолчанию все
    задачи."""
    base = query if query is not None else db.query(Task)
    tasks = base.filter(Task.status != TaskStatus.DONE).order_by(Task.id.desc()).all()
    return apply_scope(tasks, user, scope)


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
    priority: TaskPriority = TaskPriority.MEDIUM,
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
        priority=priority,
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
    priority: TaskPriority | None = None,
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
    if priority is not None:
        task.priority = priority
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
    if completed_task.block is not None:
        # Закрытие задачи блока меняет допуск следующих блоков и, для задачи
        # сверки материала, состояние материалов (0084-c).
        readiness.invalidate_production_caches(db, completed_task.block.production_id)
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


def _assign_policy_reviewer(db: Session, task: Task, actor: User | None) -> None:
    """Задачу, требующую приёмки, сдали, а принять её некому (0084-f).
    Проверяющим становится ответственный — если он есть и сам не исполнитель
    этой задачи (свою работу он принять не может, см. policy). Иначе ничего
    не придумываем: задача остаётся «на проверке», TaskOut.review_blocked_reason
    говорит, что проверяющего нет."""
    responsible = task.responsible or (
        db.get(User, task.responsible_id) if task.responsible_id is not None else None
    )
    if responsible is None or not review_policy.can_accept_own_work(task, responsible):
        return
    task.reviewers.append(responsible)
    db.add(
        TaskReport(
            task_id=task.id,
            author_id=(actor or responsible).id,
            kind=TaskReportKind.REVIEWER_ASSIGNED,
            comment=f"Проверяющий назначен по политике: ответственный — {responsible.full_name}",
        )
    )
    db.flush()


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

    if task.status == TaskStatus.IN_REVIEW and not task.reviewers and not review_policy.requires_review(task):
        # Auto-close when there is nobody to review: not attributable to a person.
        # Задачи, требующие приёмки (0084-f, app/tasks/policy.py), так не
        # закрываются — остаются «на проверке», пока их не примут.
        return _finalize_status(db, task, TaskStatus.DONE, automatic=True)

    if task.status == TaskStatus.IN_REVIEW and not review_policy.eligible_reviewers(task):
        _assign_policy_reviewer(db, task, actor)

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
    if new_status == TaskStatus.DONE:
        _ensure_not_own_acceptance(task, actor)

    return _finalize_status(db, task, new_status, actor=actor)


def _ensure_not_own_acceptance(task: Task, actor: User) -> None:
    if not review_policy.can_accept_own_work(task, actor):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Принять свою сдачу нельзя: задачу блока производства принимает другой сотрудник",
        )


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


def add_report(
    db: Session,
    task: Task,
    actor: User,
    *,
    kind: TaskReportKind,
    comment: str,
) -> None:
    """Записать в журнал задачи строку без смены статуса — например перенос
    срока задачи по клиенту (0079-d). Файлы здесь не прикладываются: для
    сдачи с вложениями есть submit_report."""
    _add_report(db, task, actor, kind=kind, comment=comment, files=[])


def close_with_resolution(db: Session, task: Task, actor: User, *, comment: str) -> Task:
    """Закрыть задачу, записав в журнал, чем дело кончилось.

    В отличие от submit_report не требует, чтобы закрывающий был исполнителем
    и чтобы задача была ровно «в работе»: задачами по клиенту (0079-d)
    занимается тот менеджер, который сейчас ведёт карточку, а не обязательно
    тот, на кого её завели.
    """
    if task.status == TaskStatus.DONE:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Задача уже закрыта")
    _add_report(db, task, actor, kind=TaskReportKind.SUBMISSION, comment=comment, files=[])
    return _finalize_status(db, task, TaskStatus.DONE, actor=actor)


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
    поведение _finalize_status), отчёт при этом остаётся у задачи — кроме
    задач, требующих приёмки по app/tasks/policy.py (0084-f): те остаются
    «на проверке».
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
    if accept:
        # До записи в журнал: отказ не должен оставлять «принято» в отчётах.
        _ensure_not_own_acceptance(task, actor)

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
    дату отправки и вложения правка не трогает. Прежний текст перед
    перезаписью сохраняется ревизией (0084-g) с признаком «после приёмки».
    """
    report = db.get(TaskReport, report_id)
    if report is None or report.task_id != task.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Отчёт не найден")
    if report.kind == TaskReportKind.REVIEWER_ASSIGNED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Служебную запись журнала изменить нельзя",
        )
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
        now = datetime.now(timezone.utc)
        report.revisions.append(
            TaskReportRevision(
                comment=report.comment,
                edited_by_id=actor.id,
                edited_at=now,
                after_acceptance=_is_accepted_after(task, report),
            )
        )
        report.comment = text
        report.updated_at = now
        db.flush()
    return report


def get_report_or_404(db: Session, task: Task, report_id: int) -> TaskReport:
    """Запись журнала именно этой задачи — для чтения её ревизий."""
    report = db.get(TaskReport, report_id)
    if report is None or report.task_id != task.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Отчёт не найден")
    return report


def _is_accepted_after(task: Task, report: TaskReport) -> bool:
    """Приняли ли задачу уже после этой записи: задача закрыта или в журнале
    есть решение «принято», оставленное позже неё. Правка в этот момент —
    правка того, что проверяющий уже принял."""
    if task.status == TaskStatus.DONE:
        return True
    return any(
        r.kind == TaskReportKind.REVIEW_ACCEPTED and r.id > report.id
        for r in task.reports
    )


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
    description: str | None = None,
) -> Task:
    task = Task(
        title=title,
        description=description,
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
