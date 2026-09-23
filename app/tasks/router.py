from datetime import datetime, timezone

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile, status
from sqlalchemy.orm import Session

from app.common.module_access import Module
from app.core.deps import require_admin, require_edit, require_view
from app.db.session import get_db
from app.tasks import service as task_service
from app.tasks import story_points_stats
from app.tasks.models import Task, TaskLinkType, TaskStatus
from app.tasks.schemas import (
    TaskCreate,
    TaskOut,
    TaskReportCommentUpdate,
    TaskScope,
    TaskStatusUpdate,
    TaskUpdate,
    WorkloadOut,
)
from app.users.models import User

app = FastAPI(
    title="Soborbum — Задачи",
    description="Общий раздел задач сотрудников: ручные задачи и задачи, "
    "синхронизированные с производством, клиентами, складом и маркетингом.",
    version="0.1.2",
)

require_tasks_view = require_view(Module.TASKS)
require_tasks_edit = require_edit(Module.TASKS)


@app.get("/", response_model=list[TaskOut])
def list_tasks(
    db: Session = Depends(get_db),
    current: User = Depends(require_tasks_view),
    scope: TaskScope = TaskScope.MINE,
    assignee_id: int | None = None,
    reviewer_id: int | None = None,
    block_id: int | None = None,
    link_type: TaskLinkType | None = None,
    task_status: TaskStatus | None = Query(None, alias="status"),
    overdue: bool | None = None,
):
    if scope == TaskScope.ALL and not current.has_access(Module.TASKS_ALL):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Нет доступа к «Все задачи»")

    query = db.query(Task)
    if assignee_id is not None:
        query = query.filter(Task.assignees.any(User.id == assignee_id))
    if reviewer_id is not None:
        query = query.filter(Task.reviewers.any(User.id == reviewer_id))
    if block_id is not None:
        query = query.filter(Task.block_id == block_id)
    if link_type is not None:
        query = query.filter(Task.link_type == link_type)
    if task_status is not None:
        query = query.filter(Task.status == task_status)
    tasks = query.order_by(Task.id.desc()).all()

    if scope == TaskScope.MINE:
        tasks = [t for t in tasks if task_service.is_mine_or_claimable(t, current)]
    elif scope == TaskScope.CLAIMABLE:
        tasks = [t for t in tasks if task_service.is_claimable_for(t, current)]

    if overdue:
        now = datetime.now(timezone.utc)
        tasks = [t for t in tasks if t.deadline and t.deadline < now and t.status != TaskStatus.DONE]
    return [TaskOut.from_model(t) for t in tasks]


@app.post("/", response_model=TaskOut, status_code=status.HTTP_201_CREATED)
def create_task(payload: TaskCreate, db: Session = Depends(get_db), _: User = Depends(require_tasks_edit)):
    task = task_service.create_task(
        db,
        title=payload.title,
        description=payload.description,
        deadline=payload.deadline,
        priority=payload.priority,
        assignee_ids=payload.assignee_ids,
        reviewer_ids=payload.reviewer_ids,
        responsible_id=payload.responsible_id,
        depends_on_ids=payload.depends_on_ids,
        image_ids=payload.image_ids,
        block_id=payload.block_id,
    )
    db.commit()
    db.refresh(task)
    return TaskOut.from_model(task)


@app.get("/workload", response_model=list[WorkloadOut])
def workload(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    return story_points_stats.workload_by_user(db)


@app.get("/{task_id}", response_model=TaskOut)
def get_task(task_id: int, db: Session = Depends(get_db), _: User = Depends(require_tasks_view)):
    return TaskOut.from_model(task_service.get_task_or_404(db, task_id))


@app.patch("/{task_id}", response_model=TaskOut)
def update_task(task_id: int, payload: TaskUpdate, db: Session = Depends(get_db), _: User = Depends(require_tasks_edit)):
    task = task_service.get_task_or_404(db, task_id)
    task = task_service.update_task(
        db,
        task,
        title=payload.title,
        description=payload.description,
        deadline=payload.deadline,
        priority=payload.priority,
        assignee_ids=payload.assignee_ids,
        reviewer_ids=payload.reviewer_ids,
        responsible_id=payload.responsible_id,
        depends_on_ids=payload.depends_on_ids,
        image_ids=payload.image_ids,
    )
    db.commit()
    db.refresh(task)
    return TaskOut.from_model(task)


@app.patch("/{task_id}/status", response_model=TaskOut)
def update_task_status(
    task_id: int,
    payload: TaskStatusUpdate,
    db: Session = Depends(get_db),
    actor: User = Depends(require_tasks_edit),
):
    if payload.status == TaskStatus.IN_REVIEW:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Сдать задачу можно только с отчётом — POST /api/tasks/{id}/report",
        )
    task = task_service.get_task_or_404(db, task_id)
    task = task_service.set_status(db, task, payload.status, actor)
    db.commit()
    db.refresh(task)
    return TaskOut.from_model(task)


@app.post("/{task_id}/report", response_model=TaskOut)
def submit_task_report(
    task_id: int,
    comment: str = Form(..., description="Комментарий исполнителя о выполненной задаче"),
    files: list[UploadFile] = File(default=[], description="Файлы отчёта, по желанию"),
    db: Session = Depends(get_db),
    actor: User = Depends(require_tasks_edit),
):
    """Сдать задачу с отчётом: «в работе» → «на проверке» вместе с
    комментарием и файлами. Единственный способ сдать задачу вручную —
    PATCH /{task_id}/status со статусом in_review отклоняется."""
    task = task_service.get_task_or_404(db, task_id)
    task = task_service.submit_report(db, task, actor, comment=comment, files=files)
    db.commit()
    db.refresh(task)
    return TaskOut.from_model(task)


@app.post("/{task_id}/review", response_model=TaskOut)
def review_task(
    task_id: int,
    accept: bool = Form(..., description="true — принять задачу, false — вернуть в работу"),
    comment: str = Form("", description="Комментарий проверяющего, по желанию"),
    files: list[UploadFile] = File(default=[], description="Файлы проверяющего, по желанию"),
    db: Session = Depends(get_db),
    actor: User = Depends(require_tasks_edit),
):
    """Решение проверяющего с отчётом: принять («на проверке» → «выполнена»)
    или вернуть в работу, приложив комментарий и файлы. Без комментария и
    файлов работает так же, как PATCH /{task_id}/status."""
    task = task_service.get_task_or_404(db, task_id)
    task = task_service.review_task(db, task, actor, accept=accept, comment=comment, files=files)
    db.commit()
    db.refresh(task)
    return TaskOut.from_model(task)


@app.patch("/{task_id}/reports/{report_id}", response_model=TaskOut)
def edit_report_comment(
    task_id: int,
    report_id: int,
    payload: TaskReportCommentUpdate,
    db: Session = Depends(get_db),
    actor: User = Depends(require_tasks_edit),
):
    """Поправить текст своего комментария в журнале отчётов. Доступно в любом
    статусе задачи, включая уже принятую."""
    task = task_service.get_task_or_404(db, task_id)
    task_service.edit_report_comment(db, task, report_id, actor, payload.comment)
    db.commit()
    db.refresh(task)
    return TaskOut.from_model(task)


@app.post("/{task_id}/claim", response_model=TaskOut)
def claim_task(task_id: int, db: Session = Depends(get_db), current: User = Depends(require_tasks_edit)):
    task = task_service.get_task_or_404(db, task_id)
    task = task_service.claim_task(db, task, current)
    db.commit()
    db.refresh(task)
    return TaskOut.from_model(task)


@app.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_task(task_id: int, db: Session = Depends(get_db), _: User = Depends(require_tasks_edit)):
    task = task_service.get_task_or_404(db, task_id)
    if task.link_type != TaskLinkType.NONE or task.block_id is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Нельзя удалить задачу, синхронизированную с другим разделом",
        )
    db.delete(task)
    db.commit()
