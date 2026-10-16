"""Рабочее пространство автономного агента (0112): журнал, приоритеты, наблюдения, счётчики.

Отдельные таблицы от бизнес-данных. Агент пишет сюда и публикует результат в уже
существующие точки (уведомления, задачи, очередь внимания «Пульса»), а не в чужие разделы.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

LEVEL_A = "A"
LEVEL_B = "B"

WATCH_OPEN = "open"
WATCH_ESCALATED = "escalated"
WATCH_RESOLVED = "resolved"


class AgentAutoRun(Base):
    """Одна запись журнала: один прогон агента."""

    __tablename__ = "agent_auto_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    trigger: Mapped[str] = mapped_column(String(16), nullable=False, default="schedule")
    sections: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    # [{"section","scenario","key","text"}]
    deviations: Mapped[list[dict]] = mapped_column(JSON, nullable=False, default=list)
    # [{"scenario","key","level","result","text","task_id"?}]
    actions: Mapped[list[dict]] = mapped_column(JSON, nullable=False, default=list)
    # {section: {metric: value}} — с чем сравниваем следующий прогон.
    signals: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class AgentPriority(Base):
    __tablename__ = "agent_priorities"

    section: Mapped[str] = mapped_column(String(32), primary_key=True)
    weight: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    # Прогонов подряд с сигналом / тишиной в разделе.
    signal_streak: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    quiet_streak: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AgentWatch(Base):
    """Открытое наблюдение: то, что не решается за один прогон."""

    __tablename__ = "agent_watchlist"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    section: Mapped[str] = mapped_column(String(32), nullable=False)
    scenario: Mapped[str] = mapped_column(String(32), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=WATCH_OPEN, index=True)
    seen_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    escalate_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class AgentActionCounter(Base):
    """Сколько раз подряд А-действие сценария выполнено без реакции человека."""

    __tablename__ = "agent_action_counters"

    scenario: Mapped[str] = mapped_column(String(32), primary_key=True)
    repeats: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_action_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_reaction_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
