from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.warehouse.models import (
    IssueDestinationKind,
    MaterialCategory,
    StockMovementReason,
    SupplierStatus,
    Warehouse,
    WarehouseOperationKind,
)


class MaterialCharacteristics(BaseModel):
    """Характеристики позиции (0078) — все необязательные."""

    kind: str | None = Field(default=None, max_length=120)
    size: str | None = Field(default=None, max_length=120)
    diameter: str | None = Field(default=None, max_length=60)
    serial_number: str | None = Field(default=None, max_length=120)
    pack_quantity: float | None = Field(default=None, gt=0)
    supplier_id: int | None = None


class WarehouseMaterialCreate(MaterialCharacteristics):
    warehouse: Warehouse
    category: MaterialCategory = MaterialCategory.NONE
    title: str
    code: str
    unit: str
    is_fractional: bool = False
    quantity_in_stock: float = 0
    purchase_price: float = 0
    threshold: float = 0


class WarehouseMaterialUpdate(MaterialCharacteristics):
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
    kind: str | None = None
    size: str | None = None
    diameter: str | None = None
    serial_number: str | None = None
    pack_quantity: float | None = None
    supplier_id: int | None = None
    supplier_name: str | None = None
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
    note: str | None = None
    operation_id: int | None = None
    balance_after: float | None = None
    created_by_id: int
    created_at: datetime


class WriteOffRequest(BaseModel):
    quantity: float
    reason: str


# --- Документы операций склада и журнал (0088) ---


class OperationLineIn(BaseModel):
    warehouse_material_id: int
    quantity: float


class InventoryOperationCreate(BaseModel):
    """Оприходование / списание по итогам контроля остатков. Причина обязательна
    (проверяется в сервисе, чтобы отказ был понятной фразой, а не 422)."""

    occurred_at: datetime | None = None
    note: str = Field(default="", max_length=500)
    lines: list[OperationLineIn]


class OperationLineOut(BaseModel):
    movement_id: int
    warehouse_material_id: int
    material_title: str
    material_code: str
    unit: str
    delta: float
    balance_after: float | None


class WarehouseOperationOut(BaseModel):
    id: int
    kind: WarehouseOperationKind
    occurred_at: datetime
    destination_kind: IssueDestinationKind | None
    destination: str | None
    production_id: int | None
    received_by: str | None
    note: str | None
    created_by_id: int
    created_by_name: str
    created_at: datetime
    lines: list[OperationLineOut]


class JournalEntryOut(BaseModel):
    """Строка журнала = одно движение остатка + сведения его документа.
    У движений до 0088 и у поставок/заявок производства документа нет —
    operation_id и поля «куда/кто получил» пустые (для заявки «куда» = дом)."""

    movement_id: int
    operation_id: int | None
    occurred_at: datetime
    reason: StockMovementReason
    warehouse_material_id: int
    material_title: str
    material_code: str
    unit: str
    delta: float
    balance_after: float | None
    destination_kind: IssueDestinationKind | None
    destination: str | None
    production_id: int | None
    received_by: str | None
    note: str | None
    created_by_name: str


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


class SupplierNoteCreate(BaseModel):
    text: str


class SupplierNoteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    supplier_id: int
    author_id: int
    author_name: str | None = None
    text: str
    created_at: datetime


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
    notes: list[SupplierNoteOut] = []
    # Взаиморасчёты (0011-d): total_paid растёт при проведённой оплате
    # поставки только начиная с 0011-f; balance = total_ordered - total_paid.
    total_ordered: float = 0
    total_paid: float = 0
    balance: float = 0


class LinkMaxChatIn(BaseModel):
    chat_id: int


class PriceListImportResult(BaseModel):
    """Итог `POST /suppliers/{id}/price-items/import`."""

    supplier: SupplierOut
    imported: int
    skipped: int
    # разметку колонок сделал ИИ (True) или словарь-эвристика (False)
    ai_used: bool
    note: str = ""
    # {"material": "<заголовок>", "price": ..., "category": None, ...}
    column_mapping: dict
    # необязательные поля, для которых в файле не нашлось колонки
    missing_fields: list[str]
    # есть смысл предложить задачу «дозаполнить» (не хватает полей или есть пропуски)
    backfill_suggested: bool = False


class AiFillCategoryResult(BaseModel):
    supplier: SupplierOut
    filled: int
    skipped: int


class LeadTimeQuestionDraft(BaseModel):
    message: str
    materials: list[str]
    chat_id: int


class LeadTimeQuestionSend(BaseModel):
    message: str


class LeadTimeQuestionSent(BaseModel):
    sent: bool
    chat_id: int


class BackfillTaskRequest(BaseModel):
    missing_fields: list[str] = []


class BackfillTaskResult(BaseModel):
    task_id: int
    supplier: SupplierOut


# --- Отпуск со склада (0088-b) ---


class ManualIssueCreate(BaseModel):
    """«Куда»/«кто получил» проверяются в сервисе — отказ понятной фразой."""

    occurred_at: datetime | None = None
    destination_kind: IssueDestinationKind
    destination: str | None = Field(default=None, max_length=255)
    production_id: int | None = None
    received_by: str = Field(default="", max_length=255)
    note: str | None = Field(default=None, max_length=500)
    lines: list[OperationLineIn]


class IssueSuggestionsOut(BaseModel):
    destinations: list[str]
    received_by: list[str]


class TechcardHouseOut(BaseModel):
    production_id: int
    house_name: str
    client_name: str | None
    house_model_title: str | None
    positions_to_issue: int


class TechcardBlockShare(BaseModel):
    block_id: int
    block_name: str
    to_issue: float


class TechcardPreviewLine(BaseModel):
    warehouse_material_id: int
    material_title: str
    material_code: str
    unit: str
    is_fractional: bool
    norm_total: float
    provided: float
    requested: float
    to_issue: float
    in_stock: float
    balance_after: float
    shortage: bool
    blocks: list[TechcardBlockShare]


class TechcardPreviewOut(BaseModel):
    production_id: int
    house_label: str
    house_model_title: str | None
    # Материалы техкарты с нулевым нормативом (количество не перенесено из КР).
    zero_norm_count: int
    # Открытые задачи «сопоставить материал КР со складом» по блокам дома.
    unmatched_materials_count: int
    lines: list[TechcardPreviewLine]


class TechcardIssueCreate(BaseModel):
    occurred_at: datetime | None = None
    received_by: str = Field(default="", max_length=255)
    note: str | None = Field(default=None, max_length=500)
    # None — отпустить весь остаток норматива.
    lines: list[OperationLineIn] | None = None
