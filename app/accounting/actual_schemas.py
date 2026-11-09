from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import UUID
from pydantic import Field, model_validator
from app.accounting.evidence_schemas import Strict, Citation

CATEGORIES = {
    'materials': 'Материалы в производстве', 'work': 'Работы и подряд',
    'transport': 'Доставка и разгрузка', 'installation': 'Монтаж',
    'realtor': 'Комиссия риелтора', 'seller': 'Вознаграждение продавца',
    'advertising': 'Реклама проекта', 'factory': 'Цех', 'warehouse': 'Склад',
    'staff': 'Штатные сотрудники', 'other_fixed': 'Прочие постоянные расходы',
    'tax': 'Налоги и сборы', 'other': 'Прочие затраты проекта',
}
FIXED = {'factory', 'warehouse', 'staff', 'other_fixed'}


class Allocation(Strict):
    category: Literal['materials','work','transport','installation','realtor','seller','advertising','factory','warehouse','staff','other_fixed','tax','other']
    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=2, allow_inf_nan=False)
    project_id: int | None = Field(None, gt=0)
    month: date | None = None
    locator: str = Field(min_length=3, max_length=500)

    @model_validator(mode='after')
    def target(self):
        if self.category in FIXED:
            if self.project_id is not None or self.month is None or self.month.day != 1:
                raise ValueError('Постоянные затраты относятся к календарному месяцу')
        elif self.project_id is None or self.month is not None:
            raise ValueError('Прямой расход относится к проекту / смете')
        return self


class CostInput(Strict):
    request_id: UUID
    expected_version: int = Field(0, ge=0)
    kind: Literal['document','material']
    organization_id: int = Field(gt=0)
    issuer: str = Field(min_length=3, max_length=255)
    number: str = Field(min_length=1, max_length=255)
    document_date: date
    line_ref: str = Field(min_length=1, max_length=120)
    incurred_on: date
    evidence: Citation
    gross_amount: Decimal = Field(gt=0, max_digits=18, decimal_places=2, allow_inf_nan=False)
    excluded_tax: Decimal = Field(ge=0, max_digits=18, decimal_places=2, allow_inf_nan=False)
    tax_basis: str = Field(min_length=3, max_length=1000)
    allocations: list[Allocation] = Field(min_length=1, max_length=100)
    receipt_id: int | None = Field(None, gt=0)
    receipt_line: int | None = Field(None, ge=0)
    movement_id: int | None = Field(None, gt=0)
    quantity: Decimal | None = Field(None, gt=0, le=10000000, max_digits=17, decimal_places=6, allow_inf_nan=False)
    reason: str = Field(min_length=3, max_length=2000)

    @model_validator(mode='after')
    def amounts(self):
        if self.excluded_tax >= self.gross_amount:
            raise ValueError('Исключаемый налог должен быть меньше полной суммы')
        if sum((a.amount for a in self.allocations), Decimal(0)) != self.gross_amount-self.excluded_tax:
            raise ValueError('Распределите ровно сумму затрат после исключаемого налога')
        if self.kind == 'material':
            if None in (self.receipt_id,self.receipt_line,self.movement_id,self.quantity):
                raise ValueError('Выберите партию поступления, выдачу и количество')
            if len(self.allocations)!=1 or self.allocations[0].category!='materials':
                raise ValueError('Выдача материала относится к одному проекту')
        elif any(x is not None for x in (self.receipt_id,self.receipt_line,self.movement_id,self.quantity)) or any(a.category=='materials' for a in self.allocations):
            raise ValueError('Материалы признаются только по поступлению и выдаче')
        if self.incurred_on < self.document_date and self.kind=='material':
            raise ValueError('Дата выдачи не может предшествовать документу поступления')
        return self


class CostDecision(Strict):
    request_id: UUID
    version: int = Field(gt=0)
    source_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    action: Literal['recognize','void']
    confirmed: Literal[True]
    reason: str = Field(min_length=3, max_length=2000)


class ZeroCost(Strict):
    category: str
    evidence: Citation


class ClosureInput(Strict):
    request_id: UUID
    source_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    confirmed: Literal[True]
    reason: str = Field(min_length=3, max_length=2000)
    completion_evidence: Citation
    zero_categories: list[ZeroCost] = Field(max_length=20)
    production_ids: list[int] = Field(max_length=100)
    shipment_ids: list[int] = Field(min_length=1, max_length=100)
    revenue_evidence: Citation
    revenue_excluded_tax: Decimal = Field(ge=0,max_digits=18,decimal_places=2,allow_inf_nan=False)
    revenue_tax_basis: str = Field(min_length=3,max_length=1000)


class DirectorClosure(Strict):
    request_id: UUID
    accounting_closure_id: int = Field(gt=0)
    source_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    confirmed: Literal[True]
    reason: str = Field(min_length=3,max_length=2000)
