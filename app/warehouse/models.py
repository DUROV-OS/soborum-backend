import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    JSON,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class StockMovementReason(str, enum.Enum):
    SUPPLY = "supply"
    ISSUED = "issued"
    REQUIRED_ADJUSTED_UP = "required_adjusted_up"
    REQUEST_REJECTED_RETURN = "request_rejected_return"
    MANUAL_ADJUST = "manual_adjust"


class Warehouse(str, enum.Enum):
    TECHNOLOGY = "Склад Технология"
    ID_GROUP = "Склад ИД Групп"


class MaterialCategory(str, enum.Enum):
    NONE = "без категории"
    BEAMS_BOARDS = "брусы/доска"
    VENTILATION = "вентиляция"
    WATER_SEWAGE = "вода/канализация"
    TOOLS = "инструмент"
    ASSEMBLY_KITS = "комплекты для сборки"
    PAINT_VARNISH_OIL = "краска/лак/масло"
    ROOFING = "кровельный материал"
    FURNITURE = "мебель"
    MEMBRANE = "мембрана"
    FASTENERS = "метизы"
    WINDOWS_DOORS = "окна и двери"
    STOVE = "печное"
    CONSUMABLES = "расходные материалы"
    PILES = "сваи"
    INSULATION = "утепление и изоляции"
    UTILITY_BLOCK = "хоз.блок"
    TANKS = "чаны"
    ELECTRICAL = "электрика"


class WarehouseMaterial(Base):
    __tablename__ = "warehouse_materials"
    __table_args__ = (UniqueConstraint("warehouse", "code", name="uq_warehouse_materials_warehouse_code"),)

    id: Mapped[int] = mapped_column(primary_key=True)

    # each warehouse keeps its own materials ledger - code is unique per warehouse, not globally
    #
    # values_callable: the underlying postgres enum type stores the Russian
    # label (member .value), not the python member .name - it's what the
    # migration's CREATE TYPE lists and what the API sends/receives as JSON.
    warehouse: Mapped[Warehouse] = mapped_column(
        Enum(Warehouse, name="warehouse_name", values_callable=lambda enum_cls: [e.value for e in enum_cls]),
        nullable=False,
    )
    category: Mapped[MaterialCategory] = mapped_column(
        Enum(MaterialCategory, name="material_category", values_callable=lambda enum_cls: [e.value for e in enum_cls]),
        nullable=False,
        default=MaterialCategory.NONE,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    code: Mapped[str] = mapped_column(String(64), nullable=False)

    unit: Mapped[str] = mapped_column(String(32), nullable=False)
    is_fractional: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    quantity_in_stock: Mapped[float] = mapped_column(Numeric(14, 3, asdecimal=False), nullable=False, default=0)
    purchase_price: Mapped[float] = mapped_column(Numeric(14, 2, asdecimal=False), nullable=False, default=0)

    # kept for the automatic shortage-task feature (see service.sync_shortage_task);
    # not part of the user-facing field set, edited only via update_threshold
    threshold: Mapped[float] = mapped_column(Numeric(14, 3, asdecimal=False), nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Supply(Base):
    __tablename__ = "supplies"

    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    created_by: Mapped["User"] = relationship()  # noqa: F821
    lines: Mapped[list["SupplyLine"]] = relationship(back_populates="supply", cascade="all, delete-orphan")


class SupplyLine(Base):
    __tablename__ = "supply_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    supply_id: Mapped[int] = mapped_column(ForeignKey("supplies.id", ondelete="CASCADE"), nullable=False)
    warehouse_material_id: Mapped[int] = mapped_column(ForeignKey("warehouse_materials.id"), nullable=False)
    quantity: Mapped[float] = mapped_column(Numeric(14, 3, asdecimal=False), nullable=False)

    supply: Mapped["Supply"] = relationship(back_populates="lines")
    warehouse_material: Mapped["WarehouseMaterial"] = relationship()


class StockMovement(Base):
    __tablename__ = "stock_movements"

    id: Mapped[int] = mapped_column(primary_key=True)
    warehouse_material_id: Mapped[int] = mapped_column(ForeignKey("warehouse_materials.id"), nullable=False)
    delta: Mapped[float] = mapped_column(Numeric(14, 3, asdecimal=False), nullable=False)
    reason: Mapped[StockMovementReason] = mapped_column(
        Enum(StockMovementReason, name="stock_movement_reason"), nullable=False
    )
    reference_id: Mapped[int | None] = mapped_column(nullable=True)
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    warehouse_material: Mapped["WarehouseMaterial"] = relationship()
    created_by: Mapped["User"] = relationship()  # noqa: F821


class SupplierStatus(str, enum.Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class Supplier(Base):
    """Реестр поставщиков снабжения: контакты, привязанный чат MAX и прайс-лист
    по материалам. Единая сущность для фичи 0011 «Бухгалтерия» — поля
    взаиморасчётов/поставок добавляет дочерняя 0011-d поверх этой модели."""

    __tablename__ = "suppliers"
    __table_args__ = (UniqueConstraint("max_chat_id", name="uq_suppliers_max_chat_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # названия категорий материалов, с которыми работает поставщик (свободный список)
    categories: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[SupplierStatus] = mapped_column(
        Enum(SupplierStatus, name="supplier_status"), nullable=False, default=SupplierStatus.ACTIVE
    )
    # способы связи: [{"kind": "phone|email|messenger|website", "value": "...", "person": "..."}]
    contacts: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # ID чата MAX (app/max), привязанного к поставщику. Один чат — не более чем у
    # одного поставщика (uq_suppliers_max_chat_id). 0 — «Избранное»; id групп MAX
    # бывают большими и отрицательными → BigInteger. NULL — чат не привязан.
    max_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    price_items: Mapped[list["SupplierPriceItem"]] = relationship(
        back_populates="supplier",
        cascade="all, delete-orphan",
        order_by="SupplierPriceItem.id",
    )
    notes: Mapped[list["SupplierNote"]] = relationship(
        back_populates="supplier",
        cascade="all, delete-orphan",
        # id как вторичный ключ: несколько заметок в одну секунду (server_default
        # now()) иначе сортируются недетерминированно.
        order_by="(SupplierNote.created_at.desc(), SupplierNote.id.desc())",
    )


class SupplierPriceItem(Base):
    """Строка прайс-листа поставщика: материал, категория, цена по диапазонам
    размера партии и срок поставки. `round` — задел под принцип PROJECT.md
    «предложение хранится после минимум трёх раундов переговоров»."""

    __tablename__ = "supplier_price_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id", ondelete="CASCADE"), nullable=False)
    material: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # диапазоны партии: [{"min_qty": 0, "max_qty": 100, "price": 1234.0}, ...].
    # max_qty = null — «и больше». Хотя бы один диапазон с ценой обязателен.
    tiers: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    lead_time: Mapped[str | None] = mapped_column(String(64), nullable=True)
    round: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    supplier: Mapped["Supplier"] = relationship(back_populates="price_items")


class SupplierNote(Base):
    """Свободная заметка по поставщику: «завышает цены», «долго отвечает» и т. п.
    Не редактируется — только добавить/удалить (история не переписывается)."""

    __tablename__ = "supplier_notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id", ondelete="CASCADE"), nullable=False)
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    text: Mapped[str] = mapped_column(String(2000), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    supplier: Mapped["Supplier"] = relationship(back_populates="notes")
    author: Mapped["User"] = relationship()  # noqa: F821
