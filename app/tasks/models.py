import enum
from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Enum, ForeignKey, Integer, JSON, String, Table, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TaskStatus(str, enum.Enum):
    NOT_READY = "not_ready"
    READY = "ready"
    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"
    DONE = "done"


class TaskLinkType(str, enum.Enum):
    """What auto-created this task, if anything. Domain sections that create
    linked tasks (clients, marketing, warehouse) look up their own entity by
    `Task.link_id` when a linked task is closed - see app/tasks/sync.py."""

    NONE = "none"
    CLIENT_STAGE = "client_stage"
    # Приём остатка «после получения» для клиента с планом оплаты
    # ADVANCE_THEN_BALANCE / POST_PAYMENT (см. app.clients.models.PaymentPlan).
    CLIENT_BALANCE_PAYMENT = "client_balance_payment"
    CONTENT_STAGE = "content_stage"
    WAREHOUSE_REQUEST = "warehouse_request"
    WAREHOUSE_SHORTAGE = "warehouse_shortage"
    # Дозаполнить прайс поставщика после импорта таблицей: ИИ не нашёл в файле
    # колонок для части полей (см. app.warehouse.price_import). link_id — id
    # поставщика.
    SUPPLIER_PRICE_BACKFILL = "supplier_price_backfill"
    # Согласование проводки «Бухгалтерии» в статусе draft. Само создание/закрытие
    # такой задачи делает app/accounting интеграциями из разделов (задача 0011-f);
    # здесь — только значение enum под будущую сшивку.
    MONEY_MOVEMENT_APPROVAL = "money_movement_approval"
    # Дозаполнить проводки после импорта платежей выпиской: ИИ не определил вид
    # / нет назначения (см. app.accounting.payment_import, задача 0011-k).
    # link_meta = {movement_ids, missing_fields}.
    MONEY_MOVEMENT_BACKFILL = "money_movement_backfill"


task_assignees = Table(
    "task_assignees",
    Base.metadata,
    Column("task_id", ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True),
    Column("user_id", ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
)

task_reviewers = Table(
    "task_reviewers",
    Base.metadata,
    Column("task_id", ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True),
    Column("user_id", ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
)

task_images = Table(
    "task_images",
    Base.metadata,
    Column("task_id", ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True),
    Column("file_id", ForeignKey("file_assets.id", ondelete="CASCADE"), primary_key=True),
)

task_dependencies = Table(
    "task_dependencies",
    Base.metadata,
    Column("task_id", ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True),
    Column("depends_on_id", ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True),
)


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus, name="task_status"), nullable=False, default=TaskStatus.NOT_READY
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Set when this task belongs to a production module - see app/production/models.py.
    module_id: Mapped[int | None] = mapped_column(ForeignKey("modules.id"), nullable=True)

    link_type: Mapped[TaskLinkType] = mapped_column(
        Enum(TaskLinkType, name="task_link_type"), nullable=False, default=TaskLinkType.NONE
    )
    link_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    link_meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    module: Mapped["ProductionModule"] = relationship(back_populates="tasks")  # noqa: F821

    assignees: Mapped[list["User"]] = relationship(secondary=task_assignees)  # noqa: F821
    reviewers: Mapped[list["User"]] = relationship(secondary=task_reviewers)  # noqa: F821
    images: Mapped[list["FileAsset"]] = relationship(secondary=task_images)  # noqa: F821

    depends_on: Mapped[list["Task"]] = relationship(
        secondary=task_dependencies,
        primaryjoin=id == task_dependencies.c.task_id,
        secondaryjoin=id == task_dependencies.c.depends_on_id,
        backref="blocks",
    )


class TaskStageEvent(Base):
    """Append-only audit row: one per status transition of a task, written by
    app.tasks.timelog at the moment the transition is applied in
    app.tasks.service. Never mutated or deleted (except by the task's own
    CASCADE). The Tasks API is unchanged - this table is written as a side
    effect and read only by reporting / the AI layer.

    from_status is NULL for the row that records the task's creation. actor_id
    is the user who triggered a manual transition (assignee / reviewer) and
    NULL for automatic ones (dependency cascade, auto-close without reviewers,
    domain stage force-close); `automatic` flags the latter explicitly.
    """

    __tablename__ = "task_stage_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    from_status: Mapped[TaskStatus | None] = mapped_column(
        Enum(TaskStatus, name="task_status"), nullable=True
    )
    to_status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus, name="task_status"), nullable=False
    )
    actor_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    automatic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
        index=True,
    )

    task: Mapped["Task"] = relationship()
    actor: Mapped["User"] = relationship()  # noqa: F821


class TaskWorkDuration(Base):
    """Derived per-task rollup of TaskStageEvent, recomputed from the full
    event log on every transition (see app.tasks.timelog._recompute). One row
    per task.

    `seconds_by_status` holds the full breakdown {status_value: seconds} of
    time spent in each stage (closed intervals only); `total_working_seconds`
    is the sum of the active stages (in_progress + in_review) - "how long the
    task was actually worked on". `total_lead_seconds` is wall-clock from the
    first event (creation) to DONE and is only meaningful once `is_complete`.
    """

    __tablename__ = "task_work_durations"

    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True
    )
    first_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    total_working_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_lead_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    seconds_by_status: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    is_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
        server_default=func.now(),
    )

    task: Mapped["Task"] = relationship()
