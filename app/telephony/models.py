"""Private call archive; provider credentials never belong in these rows."""
from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class PhoneCall(Base):
    __tablename__ = 'phone_calls'
    __table_args__ = (UniqueConstraint('provider', 'external_id', name='uq_phone_call_provider'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(30), default='megafon')
    external_id: Mapped[str] = mapped_column(String(255))
    phone: Mapped[str] = mapped_column(String(40), index=True)
    direction: Mapped[str] = mapped_column(String(10))
    employee: Mapped[str] = mapped_column(String(255), default='')
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(40), default='unknown')
    client_id: Mapped[int | None] = mapped_column(ForeignKey('clients.id', ondelete='SET NULL'), index=True, nullable=True)
    crm_entity_id: Mapped[int | None] = mapped_column(ForeignKey('crm_entities.id', ondelete='SET NULL'), index=True, nullable=True)
    match_status: Mapped[str] = mapped_column(String(30), default='unmatched')
    recording_url: Mapped[str] = mapped_column(Text, default='')
    audio_ready: Mapped[bool] = mapped_column(default=False)
    processing: Mapped[str] = mapped_column(String(30), default='waiting_recording', index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str] = mapped_column(String(1000), default='')
    transcript: Mapped[str] = mapped_column(Text, default='')
    segments: Mapped[list] = mapped_column(JSON, default=list)
    analysis: Mapped[dict] = mapped_column(JSON, default=dict)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_by: Mapped[int | None] = mapped_column(ForeignKey('users.id'), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class PhoneCallAudit(Base):
    __tablename__ = 'phone_call_audit'
    id: Mapped[int] = mapped_column(primary_key=True)
    call_id: Mapped[int] = mapped_column(ForeignKey('phone_calls.id'), index=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    action: Mapped[str] = mapped_column(String(40))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
