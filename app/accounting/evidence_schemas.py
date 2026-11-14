from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CATEGORIES = {
    'materials': 'Материалы и комплектующие', 'work': 'Работы и подряд',
    'transport': 'Доставка и разгрузка', 'installation': 'Монтаж',
    'realtor': 'Комиссия риелтора', 'seller': 'Вознаграждение продавца',
    'advertising': 'Реклама', 'factory': 'Цех', 'warehouse': 'Склад',
    'staff': 'Штатные сотрудники', 'tax': 'Налоги и сборы',
    'other': 'Прочие затраты', 'not_cost': 'Не относится к затратам',
}


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class Citation(Strict):
    file_id: int = Field(gt=0)
    locator: str = Field(min_length=1, max_length=300)


class Allocation(Strict):
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2, allow_inf_nan=False)
    category: str
    scope: Literal['project', 'month']
    project_id: int | None = Field(default=None, gt=0)
    month: date | None = None
    evidence: Citation
    note: str = Field(default='', max_length=1000)

    @field_validator('category')
    @classmethod
    def known_category(cls, value):
        if value not in CATEGORIES: raise ValueError('Выберите статью')
        return value

    @model_validator(mode='after')
    def scope_matches(self):
        if self.scope == 'project' and (self.project_id is None or self.month is not None):
            raise ValueError('Для проекта выберите смету, без календарного месяца')
        if self.scope == 'month' and (self.project_id is not None or self.month is None or self.month.day != 1):
            raise ValueError('Для месяца укажите первое число календарного месяца')
        if self.category in ('factory', 'warehouse', 'staff') and self.scope != 'month':
            raise ValueError('Постоянные расходы относятся к календарному месяцу')
        return self


class EvidenceInput(Strict):
    request_id: UUID
    expected_version: int = Field(ge=0)
    source_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    nature: Literal['expense', 'advance', 'refund', 'transfer']
    payment_evidence: Citation
    allocations: list[Allocation] = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=3, max_length=2000)


class CheckInput(Strict):
    request_id: UUID
    version: int = Field(gt=0)
    confirmed: Literal[True]
    reason: str = Field(min_length=3, max_length=2000)
