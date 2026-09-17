import enum
from datetime import datetime

from sqlalchemy import Column, DateTime, Enum, ForeignKey, Integer, Numeric, String, Table, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class MaterialRequestStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class Production(Base):
    """Проект производства одного дома. Один цикл может содержать несколько
    таких проектов — по одному на каждый дом множественного заказа
    (см. app.clients.models.OrderType)."""

    __tablename__ = "productions"

    id: Mapped[int] = mapped_column(primary_key=True)
    cycle_id: Mapped[int] = mapped_column(ForeignKey("cycles.id"), nullable=False)
    house_index: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="Дом", server_default="Дом")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    cycle: Mapped["Cycle"] = relationship(back_populates="productions")  # noqa: F821
    blocks: Mapped[list["ProductionBlock"]] = relationship(
        back_populates="production", cascade="all, delete-orphan", order_by="ProductionBlock.sequence"
    )


block_dependencies = Table(
    "block_dependencies",
    Base.metadata,
    Column("block_id", ForeignKey("production_blocks.id", ondelete="CASCADE"), primary_key=True),
    Column("depends_on_id", ForeignKey("production_blocks.id", ondelete="CASCADE"), primary_key=True),
)


class ProductionBlock(Base):
    """Узел направленного графа этапов производства одного дома — не путать с
    app.common.module_access.Module, разделом доступа. Раньше назывался
    «модуль» (физический модуль дома); Арсений переосмыслил ту же сущность
    как этап производства с порядком (`sequence`) и зависимостями от других
    блоков того же `Production` (`depends_on`/`block_dependencies`)."""

    __tablename__ = "production_blocks"

    id: Mapped[int] = mapped_column(primary_key=True)
    production_id: Mapped[int] = mapped_column(ForeignKey("productions.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    production: Mapped["Production"] = relationship(back_populates="blocks")
    materials: Mapped[list["BlockMaterial"]] = relationship(back_populates="block", cascade="all, delete-orphan")
    tasks: Mapped[list["Task"]] = relationship(back_populates="block")  # noqa: F821

    # Блоки, от которых зависит этот блок (должны быть закрыты раньше).
    # `blocks` (обратная сторона) — блоки, которые зависят от этого.
    depends_on: Mapped[list["ProductionBlock"]] = relationship(
        secondary=block_dependencies,
        primaryjoin=id == block_dependencies.c.block_id,
        secondaryjoin=id == block_dependencies.c.depends_on_id,
        backref="blocks",
    )

    @property
    def depends_on_ids(self) -> list[int]:
        return [b.id for b in self.depends_on]


class BlockMaterial(Base):
    __tablename__ = "block_materials"

    id: Mapped[int] = mapped_column(primary_key=True)
    block_id: Mapped[int] = mapped_column(ForeignKey("production_blocks.id", ondelete="CASCADE"), nullable=False)
    warehouse_material_id: Mapped[int] = mapped_column(ForeignKey("warehouse_materials.id"), nullable=False)
    inventory_number: Mapped[str] = mapped_column(String(64), nullable=False)
    unit: Mapped[str] = mapped_column(String(32), nullable=False)
    quantity_required: Mapped[float] = mapped_column(Numeric(14, 3, asdecimal=False), nullable=False, default=0)
    quantity_requested: Mapped[float] = mapped_column(Numeric(14, 3, asdecimal=False), nullable=False, default=0)
    quantity_provided: Mapped[float] = mapped_column(Numeric(14, 3, asdecimal=False), nullable=False, default=0)

    block: Mapped["ProductionBlock"] = relationship(back_populates="materials")
    warehouse_material: Mapped["WarehouseMaterial"] = relationship()  # noqa: F821
    requests: Mapped[list["MaterialRequest"]] = relationship(
        back_populates="block_material", cascade="all, delete-orphan"
    )


class MaterialRequest(Base):
    __tablename__ = "material_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    block_material_id: Mapped[int] = mapped_column(
        ForeignKey("block_materials.id", ondelete="CASCADE"), nullable=False
    )
    warehouse_material_id: Mapped[int] = mapped_column(ForeignKey("warehouse_materials.id"), nullable=False)
    quantity: Mapped[float] = mapped_column(Numeric(14, 3, asdecimal=False), nullable=False)
    status: Mapped[MaterialRequestStatus] = mapped_column(
        Enum(MaterialRequestStatus, name="material_request_status"),
        nullable=False,
        default=MaterialRequestStatus.PENDING,
    )
    requested_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    decided_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id"), nullable=True)

    block_material: Mapped["BlockMaterial"] = relationship(back_populates="requests")
    warehouse_material: Mapped["WarehouseMaterial"] = relationship()  # noqa: F821
    requested_by: Mapped["User"] = relationship(foreign_keys=[requested_by_id])  # noqa: F821
    decided_by: Mapped["User"] = relationship(foreign_keys=[decided_by_id])  # noqa: F821
