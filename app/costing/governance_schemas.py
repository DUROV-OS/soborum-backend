from datetime import date
from decimal import Decimal
from typing import Literal
from pydantic import Field, model_validator
from app.costing.schemas import Strict, Money, VersionAction


class Evidence(Strict):
    key: str = Field(min_length=1, max_length=120)
    document_id: int = Field(gt=0)
    value: Money
    locator: str = Field(min_length=3, max_length=500)


class Review(VersionAction):
    evidence: list[Evidence] = Field(default_factory=list, max_length=5000)
    work_scope: str = Field('', max_length=4000)


class OverheadInput(Strict):
    month: date
    previous_version: int = Field(0, ge=0)
    planned_units: int = Field(ge=1, le=1000000)
    factory: Money
    warehouse: Money
    staff: Money
    other: Money = Decimal(0)
    note: str = Field(min_length=3, max_length=2000)

    @model_validator(mode='after')
    def mandatory_costs(self):
        if self.month.day != 1:
            raise ValueError('Укажите первое число календарного месяца')
        if self.factory <= 0 or self.staff <= 0:
            raise ValueError('Постоянные расходы на цех и штат обязательны и должны быть больше нуля')
        return self


class PolicyInput(Strict):
    production_user_id: int = Field(gt=0)
    accounting_user_id: int = Field(gt=0)
    director_user_id: int = Field(gt=0)

    @model_validator(mode='after')
    def independent(self):
        if len({self.production_user_id, self.accounting_user_id, self.director_user_id}) != 3:
            raise ValueError('Три этапа должны выполнять разные сотрудники')
        return self
