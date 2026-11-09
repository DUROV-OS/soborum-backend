"""Private call archive; provider credentials never belong in these rows."""
from datetime import datetime
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, func
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
    deal_id: Mapped[int | None] = mapped_column(ForeignKey('crm_entities.id', ondelete='SET NULL'), index=True, nullable=True)
    deal_match_status: Mapped[str] = mapped_column(String(30), default='unmatched')
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


class PhoneRoutingConfig(Base):
    __tablename__ = 'phone_routing_config'
    id: Mapped[int] = mapped_column(primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    main_phone: Mapped[str] = mapped_column(String(16), default='79066773555')
    # This cursor counts first assignments, not webhooks or repeat callers.
    cursor: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=1)
    # Ring timing/fallback is applied in the PBX, not in a contact response.
    wait_seconds: Mapped[int] = mapped_column(Integer, default=20)
    pbx_setup_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_create_leads: Mapped[bool] = mapped_column(Boolean, default=False)
    max_incoming_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    max_leads_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lead_stage_id: Mapped[int | None] = mapped_column(ForeignKey('crm_entities.id'), nullable=True)
    lead_actor_id: Mapped[int | None] = mapped_column(ForeignKey('users.id'), nullable=True)


class PhoneRoutingMember(Base):
    __tablename__ = 'phone_routing_members'
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), primary_key=True)
    pbx_user: Mapped[str] = mapped_column(String(100), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class PhoneRoutingMemory(Base):
    __tablename__ = 'phone_routing_memory'
    phone: Mapped[str] = mapped_column(String(16), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    source: Mapped[str] = mapped_column(String(20), default='assigned')
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class PhoneRoutingAudit(Base):
    __tablename__ = 'phone_routing_audit'
    id: Mapped[int] = mapped_column(primary_key=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PhoneDialRequest(Base):
    __tablename__ = 'phone_dial_requests'
    __table_args__ = (UniqueConstraint('actor_id', 'request_id', name='uq_phone_dial_request'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    request_id: Mapped[str] = mapped_column(String(36))
    phone: Mapped[str] = mapped_column(String(16))
    pbx_user: Mapped[str] = mapped_column(String(100))
    state: Mapped[str] = mapped_column(String(20), default='pending')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MAXInboundMessage(Base):
    __tablename__ = 'max_inbound_messages'
    __table_args__ = (UniqueConstraint('account_id', 'chat_id', 'message_id', name='uq_max_inbound_message'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[str] = mapped_column(String(40))
    chat_id: Mapped[str] = mapped_column(String(40))
    message_id: Mapped[str] = mapped_column(String(100))
    entity_id: Mapped[int | None] = mapped_column(ForeignKey('crm_entities.id'), nullable=True, index=True)
    state: Mapped[str] = mapped_column(String(30))
    phone: Mapped[str] = mapped_column(String(16), default='')
    sender_name: Mapped[str] = mapped_column(String(255), default='')
    message_text: Mapped[str] = mapped_column(Text, default='')
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
