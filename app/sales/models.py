"""Customer orders and shipments. Imported history never posts stock again."""

from datetime import datetime, date
from decimal import Decimal
from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    JSON,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class SalesOrder(Base):
    __tablename__ = "sales_orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    source_record_id: Mapped[int | None] = mapped_column(
        ForeignKey("moysklad_records.id"), unique=True
    )
    request_id: Mapped[str | None] = mapped_column(String(36), unique=True)
    parameters: Mapped[dict] = mapped_column(JSON, default=dict)
    number: Mapped[str] = mapped_column(String(255), default="")
    counterparty_id: Mapped[int] = mapped_column(
        ForeignKey("counterparties.id"), index=True
    )
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    status: Mapped[str] = mapped_column(String(20), default="draft")
    source_state: Mapped[str | None] = mapped_column(String(255))
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(default=1)
    currency: Mapped[str] = mapped_column(String(3), default="RUB")
    total: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    opening_paid: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0)
    lines: Mapped[list] = mapped_column(JSON)
    expected_at: Mapped[date | None] = mapped_column(Date)
    address: Mapped[str] = mapped_column(String(1000), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SalesShipment(Base):
    __tablename__ = "sales_shipments"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    request_id: Mapped[str | None] = mapped_column(String(36), unique=True)
    parameters: Mapped[dict] = mapped_column(JSON, default=dict)
    order_id: Mapped[int | None] = mapped_column(
        ForeignKey("sales_orders.id"), index=True
    )
    counterparty_id: Mapped[int] = mapped_column(ForeignKey("counterparties.id"))
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    number: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(20), default="draft")
    historical: Mapped[bool] = mapped_column(Boolean, default=False)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(default=1)
    currency: Mapped[str] = mapped_column(String(3), default="RUB")
    total: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    lines: Mapped[list] = mapped_column(JSON)
    received_by: Mapped[str] = mapped_column(String(255), default="")
    address: Mapped[str] = mapped_column(String(1000), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    operation_id: Mapped[int | None] = mapped_column(
        ForeignKey("warehouse_operations.id")
    )
    post_request_id: Mapped[str | None] = mapped_column(String(36), unique=True)
    post_parameters: Mapped[dict | None] = mapped_column(JSON)
    posted_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_request_id: Mapped[str | None] = mapped_column(String(36), unique=True)
    cancel_parameters: Mapped[dict | None] = mapped_column(JSON)
    cancel_operation_id: Mapped[int | None] = mapped_column(
        ForeignKey("warehouse_operations.id")
    )
    cancelled_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancellation_note: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SalesEvent(Base):
    __tablename__ = "sales_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int | None] = mapped_column(
        ForeignKey("sales_orders.id"), index=True
    )
    shipment_id: Mapped[int | None] = mapped_column(
        ForeignKey("sales_shipments.id"), index=True
    )
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    kind: Mapped[str] = mapped_column(String(32))
    data: Mapped[dict] = mapped_column(JSON)
    request_id: Mapped[str | None] = mapped_column(String(36), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
