import enum
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.common.files import FileAssetOut
from app.tasks.models import TaskLinkType, TaskStatus
from app.users.schemas import UserOut


class TaskScope(str, enum.Enum):
    """`scope` query param of GET /api/tasks/ - see app.tasks.service for the
    actual filtering logic and task 0021 for the spec."""

    MINE = "mine"
    CLAIMABLE = "claimable"
    ALL = "all"


class TaskCreate(BaseModel):
    title: str
    description: str | None = None
    deadline: datetime | None = None
    assignee_ids: list[int] = []
    reviewer_ids: list[int] = []
    depends_on_ids: list[int] = []
    image_ids: list[int] = []
    module_id: int | None = None


class TaskUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    deadline: datetime | None = None
    assignee_ids: list[int] | None = None
    reviewer_ids: list[int] | None = None
    depends_on_ids: list[int] | None = None
    image_ids: list[int] | None = None


class TaskStatusUpdate(BaseModel):
    status: TaskStatus


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    description: str | None
    deadline: datetime | None
    status: TaskStatus
    created_at: datetime
    module_id: int | None
    link_type: TaskLinkType
    link_id: int | None
    link_meta: dict | None
    assignees: list[UserOut]
    reviewers: list[UserOut]
    images: list[FileAssetOut]
    depends_on_ids: list[int]

    @staticmethod
    def from_model(task) -> "TaskOut":
        return TaskOut(
            id=task.id,
            title=task.title,
            description=task.description,
            deadline=task.deadline,
            status=task.status,
            created_at=task.created_at,
            module_id=task.module_id,
            link_type=task.link_type,
            link_id=task.link_id,
            link_meta=task.link_meta,
            assignees=[UserOut.from_model(u) for u in task.assignees],
            reviewers=[UserOut.from_model(u) for u in task.reviewers],
            images=[FileAssetOut.model_validate(f) for f in task.images],
            depends_on_ids=[t.id for t in task.depends_on],
        )
