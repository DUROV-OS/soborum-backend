"""Sales feedback is append-only and never inferred from a button click."""
from datetime import datetime
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class ConversionReview(Base):
    __tablename__ = 'marketing_conversion_reviews'
    __table_args__ = (UniqueConstraint('request_id', 'version', name='uq_conversion_review_version'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey('client_lead_requests.id'), index=True)
    version: Mapped[int] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(String(30))
    reason: Mapped[str] = mapped_column(String(1000))
    facts: Mapped[dict] = mapped_column(JSON, default=dict)
    actor_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ConversionDeliveryControl(Base):
    __tablename__ = 'marketing_conversion_delivery_control'
    id: Mapped[int] = mapped_column(primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    scheduler: Mapped[str] = mapped_column(String(80), default='durov-house-2')
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ConversionDeliveryBatch(Base):
    __tablename__ = 'marketing_conversion_delivery_batches'
    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column(String(40), default='prepared')
    version: Mapped[int] = mapped_column(Integer, default=1)
    counter_id: Mapped[str] = mapped_column(String(20), default='95674381')
    target: Mapped[str] = mapped_column(String(80), default='durov_os_qualified')
    row_count: Mapped[int] = mapped_column(Integer)
    payload_sha256: Mapped[str] = mapped_column(String(64))
    platform_upload_id: Mapped[str | None] = mapped_column(String(80))
    matched_rows: Mapped[int | None] = mapped_column(Integer)
    last_note: Mapped[str] = mapped_column(String(500), default='')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ConversionDeliveryItem(Base):
    __tablename__ = 'marketing_conversion_delivery_items'
    __table_args__ = (UniqueConstraint('client_id', 'target', name='uq_conversion_delivery_client_target'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey('marketing_conversion_delivery_batches.id'), index=True)
    request_id: Mapped[int] = mapped_column(ForeignKey('client_lead_requests.id'), index=True)
    client_id: Mapped[int] = mapped_column(ForeignKey('clients.id'))
    target: Mapped[str] = mapped_column(String(80))
    payload: Mapped[dict] = mapped_column(JSON)


class ConversionDeliveryEvent(Base):
    __tablename__ = 'marketing_conversion_delivery_events'
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int | None] = mapped_column(ForeignKey('marketing_conversion_delivery_batches.id'), index=True)
    actor: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(50))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
