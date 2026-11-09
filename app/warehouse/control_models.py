"""Signed warehouse handoffs and corrections; original movements are retained."""
from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class WarehouseControlDocument(Base):
    __tablename__ = 'warehouse_control_documents'
    id: Mapped[int] = mapped_column(primary_key=True)
    file_id: Mapped[int] = mapped_column(ForeignKey('file_assets.id'), unique=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WarehouseControlRecord(Base):
    __tablename__ = 'warehouse_control_records'
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(24), index=True)
    production_id: Mapped[int] = mapped_column(ForeignKey('productions.id'), index=True)
    request_id: Mapped[str] = mapped_column(String(36), unique=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    content: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WarehouseInventoryAct(Base):
    __tablename__ = 'warehouse_inventory_acts'
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey('stock_documents.id'), index=True)
    stage: Mapped[str] = mapped_column(String(16))
    request_id: Mapped[str] = mapped_column(String(36), unique=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    file_id: Mapped[int] = mapped_column(ForeignKey('file_assets.id'))
    content: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
