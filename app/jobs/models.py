"""Отметки плановой проверки сроков задач (0080-c).

Единственная модель здесь — `TaskDeadlineAlert`: без неё повторный прогон
`app.jobs.deadlines.check_task_deadlines` плодил бы по новому уведомлению
на каждую ещё не закрытую просроченную/скоро-просроченную задачу при каждом
запуске проверки."""

import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class DeadlineThreshold(str, enum.Enum):
    """Какой порог по сроку задачи уже отработан."""

    DUE_SOON = "due_soon"
    OVERDUE = "overdue"


class TaskDeadlineAlert(Base):
    """«По этой задаче и этому порогу уведомление уже создано» — хранится
    даже если получателя не было (`Task.responsible_id is None`), чтобы
    следующий прогон не пересчитывал эту задачу заново."""

    __tablename__ = "task_deadline_alerts"
    __table_args__ = (UniqueConstraint("task_id", "threshold", name="uq_task_deadline_alert"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    threshold: Mapped[DeadlineThreshold] = mapped_column(
        Enum(DeadlineThreshold, name="task_deadline_threshold"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    task: Mapped["Task"] = relationship()  # noqa: F821
