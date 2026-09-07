from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class AgentRun(Base):
    """One coordinator turn: legal scan, route, reply. No fake seed rows."""

    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    trace_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    reply: Mapped[str] = mapped_column(Text, nullable=False)
    legal_verdict: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    legal_rules: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    legal_passport: Mapped[str] = mapped_column(Text, nullable=False)
    released: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    specialists: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    scores: Mapped[dict[str, int]] = mapped_column(JSON, nullable=False)
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    created_by = relationship("User")


class AgentShift(Base):
    """One company tick: all eight roles, cross-review, approval queue."""

    __tablename__ = "agent_shifts"

    id: Mapped[int] = mapped_column(primary_key=True)
    verdict: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    claude_used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    items: Mapped[list["AgentShiftItem"]] = relationship(
        back_populates="shift", cascade="all, delete-orphan", order_by="AgentShiftItem.id"
    )
    approvals: Mapped[list["AgentApproval"]] = relationship(
        back_populates="shift", cascade="all, delete-orphan", order_by="AgentApproval.id"
    )


class AgentShiftItem(Base):
    __tablename__ = "agent_shift_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    shift_id: Mapped[int] = mapped_column(ForeignKey("agent_shifts.id", ondelete="CASCADE"), nullable=False)
    agent_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    daily_question: Mapped[str] = mapped_column(Text, nullable=False)
    stance: Mapped[str] = mapped_column(Text, nullable=False)
    citations: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    legal_verdict: Mapped[str] = mapped_column(String(32), nullable=False)
    reviews: Mapped[list[dict]] = mapped_column(JSON, nullable=False)

    shift: Mapped[AgentShift] = relationship(back_populates="items")


class AgentApproval(Base):
    __tablename__ = "agent_approvals"

    id: Mapped[int] = mapped_column(primary_key=True)
    shift_id: Mapped[int] = mapped_column(ForeignKey("agent_shifts.id", ondelete="CASCADE"), nullable=False)
    item_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_shift_items.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    shift: Mapped[AgentShift] = relationship(back_populates="approvals")
