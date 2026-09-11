from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.common.module_access import Module
from app.core.deps import require_module
from app.db.session import get_db
from app.tasks import service as task_service
from app.tasks.models import Task, TaskLinkType, TaskStatus
from app.tasks.schemas import TaskCreate, TaskOut, TaskScope, TaskStatusUpdate, TaskUpdate
from app.users.models import User

app = FastAPI(
    title="Soborbum — Задачи",
    description="Общий раздел задач сотрудников: ручные задачи и задачи, "
    "синхронизированные с производством, клиентами, складом и маркетингом.",
    version="0.1.2",
)

require_tasks = require_module(Module.TASKS)


@app.get("/", response_model=list[TaskOut])
def list_tasks(
    db: Session = Depends(get_db),
    current: User = Depends(require_tasks),
    scope: TaskScope = TaskScope.MINE,
    assignee_id: int | None = None,
    reviewer_id: int | None = None,
    module_id: int | None = None,
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
    if module_id is not None:
        query = query.filter(Task.module_id == module_id)
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
def create_task(payload: TaskCreate, db: Session = Depends(get_db), _: User = Depends(require_tasks)):
    task = task_service.create_task(
        db,
        title=payload.title,
        description=payload.description,
        deadline=payload.deadline,
        assignee_ids=payload.assignee_ids,
        reviewer_ids=payload.reviewer_ids,
        depends_on_ids=payload.depends_on_ids,
        image_ids=payload.image_ids,
        module_id=payload.module_id,
    )
    db.commit()
    db.refresh(task)
    return TaskOut.from_model(task)


@app.get("/{task_id}", response_model=TaskOut)
def get_task(task_id: int, db: Session = Depends(get_db), _: User = Depends(require_tasks)):
    return TaskOut.from_model(task_service.get_task_or_404(db, task_id))


@app.patch("/{task_id}", response_model=TaskOut)
def update_task(task_id: int, payload: TaskUpdate, db: Session = Depends(get_db), _: User = Depends(require_tasks)):
    task = task_service.get_task_or_404(db, task_id)
    task = task_service.update_task(
        db,
        task,
        title=payload.title,
        description=payload.description,
        deadline=payload.deadline,
        assignee_ids=payload.assignee_ids,
        reviewer_ids=payload.reviewer_ids,
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
    actor: User = Depends(require_tasks),
):
    task = task_service.get_task_or_404(db, task_id)
    task = task_service.set_status(db, task, payload.status, actor)
    db.commit()
    db.refresh(task)
    return TaskOut.from_model(task)


@app.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_task(task_id: int, db: Session = Depends(get_db), _: User = Depends(require_tasks)):
    task = task_service.get_task_or_404(db, task_id)
    if task.link_type != TaskLinkType.NONE or task.module_id is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Нельзя удалить задачу, синхронизированную с другим разделом",
        )
    db.delete(task)
    db.commit()
