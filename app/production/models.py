import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, JSON, Numeric, String, Text, func
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
    modules: Mapped[list["ProductionModule"]] = relationship(back_populates="production", cascade="all, delete-orphan")


class ProductionModule(Base):
    """A "модуль" of a modular house being produced - not to be confused with
    app.common.module_access.Module, the access-control section enum."""

    __tablename__ = "modules"

    id: Mapped[int] = mapped_column(primary_key=True)
    production_id: Mapped[int] = mapped_column(ForeignKey("productions.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    production: Mapped["Production"] = relationship(back_populates="modules")
    materials: Mapped[list["ModuleMaterial"]] = relationship(back_populates="module", cascade="all, delete-orphan")
    tasks: Mapped[list["Task"]] = relationship(back_populates="module")  # noqa: F821


class ModuleMaterial(Base):
    __tablename__ = "module_materials"

    id: Mapped[int] = mapped_column(primary_key=True)
    module_id: Mapped[int] = mapped_column(ForeignKey("modules.id", ondelete="CASCADE"), nullable=False)
    warehouse_material_id: Mapped[int] = mapped_column(ForeignKey("warehouse_materials.id"), nullable=False)
    inventory_number: Mapped[str] = mapped_column(String(64), nullable=False)
    unit: Mapped[str] = mapped_column(String(32), nullable=False)
    quantity_required: Mapped[float] = mapped_column(Numeric(14, 3, asdecimal=False), nullable=False, default=0)
    quantity_requested: Mapped[float] = mapped_column(Numeric(14, 3, asdecimal=False), nullable=False, default=0)
    quantity_provided: Mapped[float] = mapped_column(Numeric(14, 3, asdecimal=False), nullable=False, default=0)

    module: Mapped["ProductionModule"] = relationship(back_populates="materials")
    warehouse_material: Mapped["WarehouseMaterial"] = relationship()  # noqa: F821
    requests: Mapped[list["MaterialRequest"]] = relationship(
        back_populates="module_material", cascade="all, delete-orphan"
    )


class MaterialRequest(Base):
    __tablename__ = "material_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    module_material_id: Mapped[int] = mapped_column(
        ForeignKey("module_materials.id", ondelete="CASCADE"), nullable=False
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

    module_material: Mapped["ModuleMaterial"] = relationship(back_populates="requests")
    warehouse_material: Mapped["WarehouseMaterial"] = relationship()  # noqa: F821
    requested_by: Mapped["User"] = relationship(foreign_keys=[requested_by_id])  # noqa: F821
    decided_by: Mapped["User"] = relationship(foreign_keys=[decided_by_id])  # noqa: F821


class KrExtraction(Base):
    """Постраничный разбор КР (0066-c) одного клиента — текст + ссылка на
    рендер-изображение каждой страницы, сырьё для ИИ-генерации графа этапов
    ([[0066-d]]). Одна запись на клиента (`client_id` уникален), перезаписывается
    при повторном запуске (КР могли заменить) — не история версий."""

    __tablename__ = "kr_extractions"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, unique=True)
    # [{"page_number": int, "text": str, "image_file_id": int}, ...]
    pages: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    extracted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    client: Mapped["Client"] = relationship()  # noqa: F821
