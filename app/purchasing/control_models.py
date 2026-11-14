"""Append-only purchasing evidence and decisions."""
from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class PurchaseControlRecord(Base):
    __tablename__ = 'purchase_control_records'
    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey('suppliers.id'), index=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey('supplier_orders.id'), index=True)
    kind: Mapped[str] = mapped_column(String(24), index=True)
    request_id: Mapped[str] = mapped_column(String(36), unique=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    content: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PurchaseControlDocument(Base):
    __tablename__ = 'purchase_control_documents'
    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey('suppliers.id'), index=True)
    file_id: Mapped[int] = mapped_column(ForeignKey('file_assets.id'), unique=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
