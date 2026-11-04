from datetime import datetime, date
from decimal import Decimal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field


class Line(BaseModel):
    model_config = ConfigDict(extra="forbid")
    material_id: int | None = Field(None, gt=0)
    title: str = Field("", max_length=255)
    unit: str = Field("", max_length=32)
    stock_tracked: bool = True
    quantity: Decimal = Field(
        gt=0, max_digits=18, decimal_places=6, allow_inf_nan=False
    )
    unit_price: Decimal = Field(
        ge=0, max_digits=18, decimal_places=8, allow_inf_nan=False
    )


class OrderBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    counterparty_id: int = Field(gt=0)
    organization_id: int = Field(gt=0)
    lines: list[Line] = Field(min_length=1, max_length=500)
    expected_at: date | None = None
    address: str = Field("", max_length=1000)
    note: str = Field("", max_length=2000)


class CreateOrder(OrderBody):
    request_id: UUID


class EditOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)
    counterparty_id: int | None = Field(None, gt=0)
    organization_id: int | None = Field(None, gt=0)
    lines: list[Line] | None = Field(None, min_length=1, max_length=500)
    expected_at: date | None = None
    address: str | None = Field(None, max_length=1000)
    note: str | None = Field(None, max_length=2000)


class Version(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)


class Archive(Version):
    archived: bool


class Cancel(Version):
    request_id: UUID
    note: str = Field(min_length=1, max_length=500)


class ShipLine(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_line: int = Field(ge=0)
    quantity: Decimal = Field(
        gt=0, max_digits=18, decimal_places=6, allow_inf_nan=False
    )


class CreateShipment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    order_id: int = Field(gt=0)
    order_version: int = Field(ge=1)
    occurred_at: datetime
    received_by: str = Field(min_length=1, max_length=255)
    address: str = Field("", max_length=1000)
    note: str = Field("", max_length=500)
    lines: list[ShipLine] = Field(min_length=1, max_length=500)


class PostShipment(Version):
    request_id: UUID
    order_version: int = Field(ge=1)
