"""Explicit links between sales CRM cards and shared MAX chats."""
from datetime import datetime
from sqlalchemy import BigInteger, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class CRMChatLink(Base):
    __tablename__ = 'crm_chat_links'
    __table_args__ = (UniqueConstraint('max_chat_id', name='uq_crm_chat_links_max_chat_id'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey('crm_entities.id', ondelete='CASCADE'), index=True)
    max_chat_id: Mapped[int] = mapped_column(BigInteger)
    label: Mapped[str] = mapped_column(String(255))
    created_by_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
