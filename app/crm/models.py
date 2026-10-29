"""Local sales workspace. Source snapshots remain separate from editable data."""

from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, UniqueConstraint, Boolean, func
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class CRMEntity(Base):
    __tablename__ = "crm_entities"
    __table_args__ = (UniqueConstraint("kind", "source_id", name="uq_crm_source"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(30), index=True)
    source_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    name: Mapped[str] = mapped_column(String(1000))
    archived: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CRMEvent(Base):
    __tablename__ = "crm_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("crm_entities.id"), index=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(50))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
