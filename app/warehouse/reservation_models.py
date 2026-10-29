from datetime import datetime
from decimal import Decimal
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    Numeric,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class StockReservation(Base):
    __tablename__ = "stock_reservations"
    __table_args__ = (
        CheckConstraint(
            "quantity > 0 AND released_quantity >= 0 AND consumed_quantity >= 0 AND released_quantity + consumed_quantity <= quantity",
            name="ck_stock_reservations_quantities",
        ),
        CheckConstraint(
            "production_id IS NULL OR source_order_id IS NULL",
            name="ck_stock_reservations_target",
        ),
        CheckConstraint(
            "(source_order_id IS NULL AND source_line IS NULL) OR (source_order_id IS NOT NULL AND source_line IS NOT NULL)",
            name="ck_stock_reservations_order_line",
        ),
        CheckConstraint(
            "NOT archived OR quantity = released_quantity + consumed_quantity",
            name="ck_stock_reservations_archive",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    material_id: Mapped[int] = mapped_column(
        ForeignKey("warehouse_materials.id"), index=True
    )
    production_id: Mapped[int | None] = mapped_column(
        ForeignKey("productions.id"), index=True
    )
    source_order_id: Mapped[int | None] = mapped_column(
        ForeignKey("moysklad_records.id"), index=True
    )
    source_line: Mapped[int | None] = mapped_column(Integer)
    purpose: Mapped[str] = mapped_column(String(255))
    unit: Mapped[str] = mapped_column(String(32))
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    released_quantity: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=0)
    consumed_quantity: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=0)
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    request_id: Mapped[str] = mapped_column(String(36), unique=True)
    parameters: Mapped[dict] = mapped_column(JSON)
    version: Mapped[int] = mapped_column(Integer, default=1)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ReservationEvent(Base):
    __tablename__ = "stock_reservation_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    reservation_id: Mapped[int] = mapped_column(
        ForeignKey("stock_reservations.id"), index=True
    )
    kind: Mapped[str] = mapped_column(String(20))
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    request_id: Mapped[str | None] = mapped_column(String(36), unique=True)
    parameters: Mapped[dict] = mapped_column(JSON, default=dict)
    movement_id: Mapped[int | None] = mapped_column(ForeignKey("stock_movements.id"))
    note: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
