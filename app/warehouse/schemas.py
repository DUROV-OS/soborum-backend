from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.warehouse.models import MaterialCategory, StockMovementReason, SupplierStatus, Warehouse


class WarehouseMaterialCreate(BaseModel):
    warehouse: Warehouse
    category: MaterialCategory = MaterialCategory.NONE
    title: str
    code: str
    unit: str
    is_fractional: bool = False
    quantity_in_stock: float = 0
    purchase_price: float = 0
    threshold: float = 0


class WarehouseMaterialUpdate(BaseModel):
    category: MaterialCategory | None = None
    title: str | None = None
    code: str | None = None
    unit: str | None = None
    is_fractional: bool | None = None
    purchase_price: float | None = None
    threshold: float | None = None


class RequestBreakdownItem(BaseModel):
    module_id: int
    module_name: str
    production_id: int
    quantity_requested: float


class WarehouseMaterialOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    warehouse: Warehouse
    category: MaterialCategory
    title: str
    code: str
    unit: str
    is_fractional: bool
    quantity_in_stock: float
    purchase_price: float
    threshold: float
    total_requested: float
    needs_supply: bool
    request_breakdown: list[RequestBreakdownItem] = []
    created_at: datetime


class SupplyLineCreate(BaseModel):
    warehouse_material_id: int
    quantity: float


class SupplyCreate(BaseModel):
    supplier_name: str | None = None
    lines: list[SupplyLineCreate]


class SupplyLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    warehouse_material_id: int
    quantity: float


class SupplyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    supplier_name: str | None
    created_by_id: int
    created_at: datetime
    lines: list[SupplyLineOut] = []


class StockMovementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    warehouse_material_id: int
    delta: float
    reason: StockMovementReason
    reference_id: int | None
    created_by_id: int
    created_at: datetime


# --- Поставщики (задача 0011-a) ---

ContactKind = Literal["phone", "email", "messenger", "website"]


class SupplierContact(BaseModel):
    kind: ContactKind
    value: str
    person: str | None = None


class PriceTier(BaseModel):
    """Один диапазон размера партии. ``max_qty = None`` — «и больше»."""

    min_qty: float = 0
    max_qty: float | None = None
    price: float


class SupplierPriceItemCreate(BaseModel):
    material: str
    category: str | None = None
    tiers: list[PriceTier] = []
    lead_time: str | None = None
    round: int | None = None


class SupplierPriceItemUpdate(BaseModel):
    material: str | None = None
    category: str | None = None
    tiers: list[PriceTier] | None = None
    lead_time: str | None = None
    round: int | None = None


class SupplierPriceItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    supplier_id: int
    material: str
    category: str | None
    tiers: list[PriceTier]
    lead_time: str | None
    round: int | None
    created_at: datetime
    updated_at: datetime


class SupplierCreate(BaseModel):
    name: str
    categories: list[str] = []
    status: SupplierStatus = SupplierStatus.ACTIVE
    contacts: list[SupplierContact] = []


class SupplierUpdate(BaseModel):
    name: str | None = None
    categories: list[str] | None = None
    status: SupplierStatus | None = None
    contacts: list[SupplierContact] | None = None


class SupplierOut(BaseModel):
    id: int
    name: str
    categories: list[str]
    status: SupplierStatus
    contacts: list[SupplierContact]
    max_chat_id: int | None
    created_at: datetime
    price_items: list[SupplierPriceItemOut] = []
    price_items_count: int = 0


class LinkMaxChatIn(BaseModel):
    chat_id: int
