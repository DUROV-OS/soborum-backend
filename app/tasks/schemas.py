import enum
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.common.files import FileAssetOut
from app.tasks import policy as task_policy
from app.tasks.models import TaskLinkType, TaskReportKind, TaskStatus
from app.users.schemas import UserOut


class TaskScope(str, enum.Enum):
    """`scope` query param of GET /api/tasks/ - see app.tasks.service for the
    actual filtering logic and task 0021 for the spec."""

    MINE = "mine"
    CLAIMABLE = "claimable"
    ALL = "all"


# `status` query param of GET /api/tasks/: any TaskStatus plus "open" (everything
# except done) - the same set the Pulse counter counts, see task 0084-h.
TaskStatusFilter = enum.Enum(
    "TaskStatusFilter",
    {**{s.name: s.value for s in TaskStatus}, "OPEN": "open"},
    type=str,
)


class TaskCreate(BaseModel):
    title: str
    description: str | None = None
    deadline: datetime | None = None
    assignee_ids: list[int] = []
    reviewer_ids: list[int] = []
    responsible_id: int | None = None
    depends_on_ids: list[int] = []
    image_ids: list[int] = []
    block_id: int | None = None


class TaskUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    deadline: datetime | None = None
    assignee_ids: list[int] | None = None
    reviewer_ids: list[int] | None = None
    responsible_id: int | None = None
    depends_on_ids: list[int] | None = None
    image_ids: list[int] | None = None


class TaskStatusUpdate(BaseModel):
    status: TaskStatus


class TaskReportCommentUpdate(BaseModel):
    comment: str


class TaskReportOut(BaseModel):
    """Запись журнала отчётов задачи: сдача исполнителя или решение
    проверяющего с комментарием и файлами (0077)."""

    id: int
    author: UserOut
    kind: TaskReportKind
    comment: str
    created_at: datetime
    # Не None, если автор правил комментарий уже после отправки.
    updated_at: datetime | None
    files: list[FileAssetOut]
    # Сколько прежних версий текста сохранено (0084-g) и была ли среди правок
    # хоть одна после приёмки задачи. Сами версии —
    # GET /api/tasks/{task_id}/reports/{report_id}/revisions.
    revisions_count: int
    edited_after_acceptance: bool

    @staticmethod
    def from_model(report) -> "TaskReportOut":
        return TaskReportOut(
            id=report.id,
            author=UserOut.from_model(report.author),
            kind=report.kind,
            comment=report.comment,
            created_at=report.created_at,
            updated_at=report.updated_at,
            files=[FileAssetOut.model_validate(f) for f in report.files],
            revisions_count=len(report.revisions),
            edited_after_acceptance=any(r.after_acceptance for r in report.revisions),
        )


class TaskReportRevisionOut(BaseModel):
    """Прежняя версия текста записи журнала отчётов: каким был комментарий
    до правки, кто и когда его поправил (0084-g)."""

    id: int
    comment: str
    edited_by: UserOut | None
    edited_at: datetime
    after_acceptance: bool

    @staticmethod
    def from_model(revision) -> "TaskReportRevisionOut":
        return TaskReportRevisionOut(
            id=revision.id,
            comment=revision.comment,
            edited_by=UserOut.from_model(revision.edited_by) if revision.edited_by else None,
            edited_at=revision.edited_at,
            after_acceptance=revision.after_acceptance,
        )


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    description: str | None
    deadline: datetime | None
    status: TaskStatus
    created_at: datetime
    block_id: int | None
    link_type: TaskLinkType
    link_id: int | None
    link_meta: dict | None
    assignees: list[UserOut]
    reviewers: list[UserOut]
    responsible: UserOut | None
    images: list[FileAssetOut]
    reports: list[TaskReportOut]
    depends_on_ids: list[int]
    # Политика приёмки (0084-f, app/tasks/policy.py): review_required — сдача
    # без проверяющего задачу не закрывает, исполнитель не принимает свою сдачу.
    review_policy: task_policy.ReviewPolicy
    # "no_reviewer" — задача на проверке, но принять её некому (проверяющих нет
    # или это только исполнители задачи с review_required). None — всё в порядке.
    review_blocked_reason: str | None

    @staticmethod
    def from_model(task) -> "TaskOut":
        return TaskOut(
            id=task.id,
            title=task.title,
            description=task.description,
            deadline=task.deadline,
            status=task.status,
            created_at=task.created_at,
            block_id=task.block_id,
            link_type=task.link_type,
            link_id=task.link_id,
            link_meta=task.link_meta,
            assignees=[UserOut.from_model(u) for u in task.assignees],
            reviewers=[UserOut.from_model(u) for u in task.reviewers],
            responsible=UserOut.from_model(task.responsible) if task.responsible else None,
            images=[FileAssetOut.model_validate(f) for f in task.images],
            reports=[TaskReportOut.from_model(r) for r in task.reports],
            depends_on_ids=[t.id for t in task.depends_on],
            review_policy=task_policy.review_policy(task),
            review_blocked_reason=(
                "no_reviewer"
                if task.status == TaskStatus.IN_REVIEW and not task_policy.eligible_reviewers(task)
                else None
            ),
        )
