"""Local sales workspace. Source snapshots remain separate from editable data."""

from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, UniqueConstraint, Boolean, Index, LargeBinary, CheckConstraint, func
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


class CRMTaskLink(Base):
    __tablename__ = 'crm_task_links'
    entity_id: Mapped[int] = mapped_column(ForeignKey('crm_entities.id'), primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey('tasks.id'), unique=True)


class CRMSettings(Base):
    __tablename__ = 'crm_settings'
    id: Mapped[int] = mapped_column(primary_key=True)
    response_hours: Mapped[int] = mapped_column(Integer, default=24)
    idle_days: Mapped[int] = mapped_column(Integer, default=3)


class CRMImportReview(Base):
    """Append-only classification; imported source records are never rewritten."""
    __tablename__ = 'crm_import_reviews'
    __table_args__ = (Index('ix_crm_import_review_source', 'source_record_id', 'attachment_index', 'id'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    source_record_id: Mapped[int] = mapped_column(ForeignKey('moysklad_records.id'))
    attachment_index: Mapped[int] = mapped_column(Integer, default=-1)
    disposition: Mapped[str] = mapped_column(String(20))
    source_digest: Mapped[str] = mapped_column(String(64))
    content_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    public_data: Mapped[dict] = mapped_column(JSON, default=dict)
    actor_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CRMAgencyLogo(Base):
    """A sanitized image owned by one CRM agency, never a shared FileAsset/path."""
    __tablename__ = 'crm_agency_logos'
    __table_args__ = (
        CheckConstraint('width > 0 AND width <= 1024 AND height > 0 AND height <= 1024', name='ck_crm_logo_dimensions'),
        CheckConstraint('byte_size > 0 AND byte_size <= 5242880 AND length(image_data) = byte_size', name='ck_crm_logo_size'),
    )
    agency_id: Mapped[int] = mapped_column(ForeignKey('crm_entities.id'), primary_key=True)
    # Deferred bytes keep ordinary metadata queries small. No filesystem alias exists.
    image_data: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)
    sha256: Mapped[str] = mapped_column(String(64))
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    byte_size: Mapped[int] = mapped_column(Integer)
    uploaded_by_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
