"""Versioned requisites beside the existing accounting identity and supplier profile."""
from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, JSON, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class Details(Base):
    __tablename__ = 'counterparty_details'
    counterparty_id: Mapped[int] = mapped_column(ForeignKey('counterparties.id'), primary_key=True)
    version: Mapped[int] = mapped_column(default=1)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Account(Base):
    __tablename__ = 'counterparty_bank_accounts'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    counterparty_id: Mapped[int] = mapped_column(ForeignKey('counterparties.id'), index=True)
    data: Mapped[dict] = mapped_column(JSON)


class Contact(Base):
    __tablename__ = 'counterparty_contacts'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    counterparty_id: Mapped[int] = mapped_column(ForeignKey('counterparties.id'), index=True)
    data: Mapped[dict] = mapped_column(JSON)


class Change(Base):
    __tablename__ = 'counterparty_changes'
    __table_args__ = (UniqueConstraint('actor_id', 'request_id'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    counterparty_id: Mapped[int] = mapped_column(ForeignKey('counterparties.id'), index=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    request_id: Mapped[str] = mapped_column(String(36))
    request_hash: Mapped[str] = mapped_column(String(64))
    data: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Preview(Base):
    __tablename__ = 'counterparty_sync_previews'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    counterparty_id: Mapped[int] = mapped_column(ForeignKey('counterparties.id'), index=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    data: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
