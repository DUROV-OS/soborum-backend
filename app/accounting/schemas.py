from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator

from app.accounting.models import (
    MoneyAssessment,
    MoneyDirection,
    MoneyMovement,
    MoneyMovementStatus,
    MoneySourceKind,
    MoneySubkind,
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
            out.source_label = mm.supply.supplier_name or f"Поставка №{mm.supply.id}"
        return out
