from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import UUID
from pydantic import Field, model_validator
from app.accounting.evidence_schemas import Strict, Citation


class Request(Strict):
    request_id: UUID


class Trust(Request):
    expected_id: int = Field(ge=0)
    trusted: bool
    evidence: Citation
    reason: str = Field(min_length=3, max_length=1000)


class Terms(Strict):
    # Prices in supplier orders must be gross; unknown/exclusive VAT is manual.
    vat: Literal['included', 'exempt']
    vat_rate: Decimal = Field(ge=0, le=100, decimal_places=2, allow_inf_nan=False)
    delivery_per_unit: Decimal = Field(ge=0, max_digits=18, decimal_places=6, allow_inf_nan=False)
    destination: str = Field(min_length=3, max_length=255)
    payment_terms: str = Field(min_length=3, max_length=500)
    min_quantity: Decimal = Field(gt=0, le=10000000, decimal_places=6, allow_inf_nan=False)
    max_quantity: Decimal = Field(gt=0, le=10000000, decimal_places=6, allow_inf_nan=False)
    evidence: Citation

    @model_validator(mode='after')
    def valid(self):
        if self.min_quantity > self.max_quantity:
            raise ValueError('Начало диапазона партии больше конца')
        if self.vat == 'exempt' and self.vat_rate != 0:
            raise ValueError('Для цены без НДС ставка равна нулю')
        return self


class PriceLine(Terms):
    index: int = Field(ge=0, le=499)


class Price(Request):
    expected_id: int = Field(ge=0)
    version: int = Field(ge=1)
    number: str = Field(min_length=1, max_length=255)
    document_date: date
    valid_until: date
    lines: list[PriceLine] = Field(min_length=1, max_length=500)
    receipt_id: int | None = Field(None, gt=0)
    receipt_evidence: Citation | None = None

    @model_validator(mode='after')
    def valid(self):
        if self.valid_until < self.document_date:
            raise ValueError('Срок цены раньше даты документа')
        if (self.receipt_id is None) != (self.receipt_evidence is None):
            raise ValueError('Для истории закупки нужны приёмка и её документ')
        return self


class Offer(Request, Terms):
    material_id: int = Field(gt=0)
    unit: str = Field(min_length=1, max_length=32)
    currency: str = Field(pattern='^[A-Z]{3}$')
    unit_price: Decimal = Field(gt=0, max_digits=18, decimal_places=6, allow_inf_nan=False)
    number: str = Field(min_length=1, max_length=255)
    document_date: date
    valid_until: date

    @model_validator(mode='after')
    def dates(self):
        if self.valid_until < self.document_date:
            raise ValueError('Срок предложения раньше даты документа')
        return self


class Approve(Request):
    fingerprint: str = Field(pattern='^[a-f0-9]{64}$')
