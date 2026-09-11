from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, field_validator

from app.accounting.models import (
    MoneyAssessment,
    MoneyDirection,
    MoneyMovement,
    MoneyMovementStatus,
    MoneySourceKind,
    MoneySubkind,
    SupplierOrderStatus,
)


class MoneyMovementCreate(BaseModel):
    subkind: MoneySubkind
    amount: float
    currency: str = "RUB"
    tax: float = 0
    assessment: MoneyAssessment = MoneyAssessment.ACTUAL
    affects_profit: bool = True
    # По умолчанию инициатором становится текущий пользователь (проставляет роутер).
    initiator_id: int | None = None
    client_id: int | None = None
    employee_id: int | None = None
    supply_id: int | None = None
    doc_date: datetime | None = None
    payment_purpose: str | None = None
    comment: str | None = None
    external_number: str | None = None


class MoneyMovementUpdate(BaseModel):
    subkind: MoneySubkind | None = None
    amount: float | None = None
    currency: str | None = None
    tax: float | None = None
    assessment: MoneyAssessment | None = None
    affects_profit: bool | None = None
    client_id: int | None = None
    employee_id: int | None = None
    supply_id: int | None = None
    payment_purpose: str | None = None
    comment: str | None = None
    external_number: str | None = None


class MoneyMovementStatusChange(BaseModel):
    to: MoneyMovementStatus
    reason: str | None = None

    @field_validator("to")
    @classmethod
    def _forbid_draft_target(cls, value: MoneyMovementStatus) -> MoneyMovementStatus:
        if value is MoneyMovementStatus.DRAFT:
            raise ValueError("Вернуть проводку в «draft» нельзя")
        return value


class MoneyMovementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    direction: MoneyDirection
    subkind: MoneySubkind
    amount: float
    currency: str
    tax: float
    assessment: MoneyAssessment
    affects_profit: bool
    initiator_id: int
    initiator_name: str | None = None
    status: MoneyMovementStatus
    posted_at: datetime | None
    doc_date: datetime | None
    cancel_reason: str | None
    payment_purpose: str | None
    comment: str | None
    external_number: str | None
    source_kind: MoneySourceKind
    client_id: int | None
    employee_id: int | None
    supply_id: int | None
    source_label: str | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_movement(cls, mm: MoneyMovement) -> "MoneyMovementOut":
        out = cls.model_validate(mm)
        out.initiator_name = mm.initiator.full_name if mm.initiator else None
        if mm.source_kind is MoneySourceKind.CLIENT and mm.client is not None:
            out.source_label = mm.client.full_name
        elif mm.source_kind is MoneySourceKind.EMPLOYEE and mm.employee is not None:
            out.source_label = mm.employee.full_name
        elif mm.source_kind is MoneySourceKind.SUPPLY and mm.supply is not None:
            supplier_name = mm.supply.supplier.name if mm.supply.supplier else None
            out.source_label = f"{supplier_name} — заказ №{mm.supply.id}" if supplier_name else f"Заказ №{mm.supply.id}"
        return out


# --- Заказы у поставщика (задача 0011-d) ---


class SupplierOrderItem(BaseModel):
    material: str
    category: str | None = None
    quantity: float
    unit_price: float


class SupplierOrderCreate(BaseModel):
    supplier_id: int
    items: list[SupplierOrderItem]
    expected_at: date | None = None
    comment: str | None = None


class SupplierOrderUpdate(BaseModel):
    items: list[SupplierOrderItem] | None = None
    expected_at: date | None = None
    comment: str | None = None


class SupplierOrderStatusChange(BaseModel):
    to: SupplierOrderStatus


class SupplierOrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    supplier_id: int
    supplier_name: str | None = None
    items: list[SupplierOrderItem]
    total_cost: float
    currency: str
    expected_at: date | None
    status: SupplierOrderStatus
    received_at: datetime | None
    comment: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_order(cls, order) -> "SupplierOrderOut":
        out = cls.model_validate(order)
        out.supplier_name = order.supplier.name if order.supplier else None
        return out


# --- Импорт платежей таблицей (задача 0011-k) ---


class MoneyMovementImportResult(BaseModel):
    """Итог `POST /money-movements/import`."""

    imported: int
    skipped: int
    ai_used: bool
    note: str = ""
    column_mapping: dict
    # некритичные поля, для которых в файле не нашлось колонки (subkind / payment_purpose)
    missing_fields: list[str] = []
    # строк с контрагентом, не сопоставленным клиенту (нужен ручной источник)
    unmatched_source: int = 0
    # проводок с «предварительным» видом (other_income/other_expense) — кандидаты на ИИ-вид
    preliminary_subkind: int = 0
    created_ids: list[int] = []
    backfill_suggested: bool = False


class AiFillSubkindRequest(BaseModel):
    movement_ids: list[int] = []


class AiFillSubkindResult(BaseModel):
    updated: int
    skipped: int


class ImportBackfillRequest(BaseModel):
    movement_ids: list[int] = []
    missing_fields: list[str] = []


class ImportBackfillResult(BaseModel):
    task_id: int
