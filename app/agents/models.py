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
