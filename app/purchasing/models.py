from datetime import datetime
from sqlalchemy import Boolean, DateTime, ForeignKey, JSON, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class PurchaseReceipt(Base):
    __tablename__ = "purchase_receipts"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    request_id: Mapped[str | None] = mapped_column(String(36), unique=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("supplier_orders.id"), index=True)
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id"))
    operation_id: Mapped[int | None] = mapped_column(ForeignKey("warehouse_operations.id"))
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    number: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(20), default="posted")
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lines: Mapped[list] = mapped_column(JSON, default=list)
    note: Mapped[str] = mapped_column(Text, default="")
    parameters: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PurchaseReturn(Base):
    __tablename__ = "purchase_returns"
    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[str] = mapped_column(String(36), unique=True)
    parameters: Mapped[dict] = mapped_column(JSON)
    receipt_id: Mapped[int] = mapped_column(ForeignKey("purchase_receipts.id"), index=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("supplier_orders.id"), index=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"))
    operation_id: Mapped[int | None] = mapped_column(ForeignKey("warehouse_operations.id"))
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    number: Mapped[str] = mapped_column(String(255), default="")
    amount: Mapped[float] = mapped_column(Numeric(18, 2, asdecimal=False))
    currency: Mapped[str] = mapped_column(String(3), default="RUB")
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    cancelled: Mapped[bool] = mapped_column(Boolean, default=False)
    cancellation_note: Mapped[str | None] = mapped_column(String(500))
    cancelled_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_request_id: Mapped[str | None] = mapped_column(String(36), unique=True)
    cancel_operation_id: Mapped[int | None] = mapped_column(ForeignKey("warehouse_operations.id"))
    version: Mapped[int] = mapped_column(default=1)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lines: Mapped[list] = mapped_column(JSON)
    note: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
