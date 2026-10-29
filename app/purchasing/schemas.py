from datetime import datetime
from decimal import Decimal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field


class ReceiveLine(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_line: int | None = Field(None, ge=0)
    material_id: int | None = Field(None, ge=1)
    quantity: Decimal = Field(gt=0, max_digits=18, decimal_places=6)
    unit_price: Decimal = Field(ge=0, max_digits=20, decimal_places=8)


class Receive(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    order_id: int | None = Field(None, ge=1)
    supplier_id: int | None = Field(None, ge=1)
    version: int | None = Field(None, ge=1)
    occurred_at: datetime
    note: str = Field("", max_length=500)
    lines: list[ReceiveLine] = Field(min_length=1, max_length=500)


class Archive(BaseModel):
    version: int = Field(ge=1)
    archived: bool


class Payment(BaseModel):
    request_id: UUID
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    account_id: int = Field(ge=1)
