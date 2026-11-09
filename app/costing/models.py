from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, JSON, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CostingSettings(Base):
    __tablename__ = "costing_settings"
    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[int] = mapped_column(default=1)
    content: Mapped[dict] = mapped_column(JSON)


class CostingMaterial(Base):
    __tablename__ = "costing_materials"
    id: Mapped[int] = mapped_column(primary_key=True)
    warehouse_material_id: Mapped[int | None] = mapped_column(ForeignKey("warehouse_materials.id"), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    category: Mapped[str] = mapped_column(String(120), default="")
    unit: Mapped[str] = mapped_column(String(32))
    price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    price_source: Mapped[str] = mapped_column(String(1000), default="")
    effective_on: Mapped[date] = mapped_column(Date)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(default=1)


class CostingItem(Base):
    __tablename__ = "costing_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)
    title: Mapped[str] = mapped_column(String(255))
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(default=1)
    content: Mapped[dict] = mapped_column(JSON)


class CostingEstimate(Base):
    __tablename__ = "costing_estimates"
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id"), index=True)
    crm_deal_id: Mapped[int | None] = mapped_column(ForeignKey("crm_entities.id"), index=True)
    sales_order_id: Mapped[int | None] = mapped_column(ForeignKey("sales_orders.id"), index=True)
    sales_order_line: Mapped[int | None] = mapped_column()
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(255))
    number: Mapped[str] = mapped_column(String(120), default="")
    version: Mapped[int] = mapped_column(default=1)
    status: Mapped[str] = mapped_column(String(16), default="draft")
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CostingRevision(Base):
    __tablename__ = "costing_revisions"
    __table_args__ = (UniqueConstraint("estimate_id", "number", name="uq_costing_revision"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    estimate_id: Mapped[int] = mapped_column(ForeignKey("costing_estimates.id"), index=True)
    number: Mapped[int] = mapped_column()
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    reason: Mapped[str] = mapped_column(String(500))
    request_id: Mapped[str] = mapped_column(String(36), unique=True)
    request_data: Mapped[dict] = mapped_column(JSON)
    snapshot: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CostingAudit(Base):
    __tablename__ = "costing_audit"
    id: Mapped[int] = mapped_column(primary_key=True)
    entity: Mapped[str] = mapped_column(String(32), index=True)
    entity_id: Mapped[int] = mapped_column(index=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(32))
    before: Mapped[dict | None] = mapped_column(JSON)
    after: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CostingPriceVersion(Base):
    __tablename__ = "costing_price_versions"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(255))
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    snapshot: Mapped[list] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CostingOrderLink(Base):
    """One approved estimate revision for one commercial order line."""
    __tablename__ = "costing_order_links"
    __table_args__ = (UniqueConstraint("order_id", "line_index", name="uq_costing_order_line"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("sales_orders.id"), index=True)
    line_index: Mapped[int] = mapped_column()
    estimate_id: Mapped[int] = mapped_column(ForeignKey("costing_estimates.id"), index=True)
    revision_id: Mapped[int] = mapped_column(ForeignKey("costing_revisions.id"))
    fingerprint: Mapped[str] = mapped_column(String(64))
    recipe_id: Mapped[int | None] = mapped_column(ForeignKey("local_recipes.id"))
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
