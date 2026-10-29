from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, JSON, String, Text, func
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
