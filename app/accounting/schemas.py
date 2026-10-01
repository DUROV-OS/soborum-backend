from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, field_validator

from app.accounting.models import (
    CounterpartyKind,
    MoneyAssessment,
    MoneyDirection,
    MoneyMovement,
    MoneyMovementStatus,
    MoneySourceKind,
    MoneySubkind,
    SupplierOrderStatus,
)
from app.common.files import FileAssetOut


def _validate_link(value: str | None) -> str | None:
    """0072-d: ссылка — пусто или похоже на URL (http/https). Без похода в сеть."""
    if not value:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if not (cleaned.startswith("http://") or cleaned.startswith("https://")):
        raise ValueError("Ссылка должна начинаться с http:// или https://")
    return cleaned



# --- Организации и банковские счета (0081-a) ---


class BankAccountCreate(BaseModel):
    organization_id: int
    name: str
    bank_name: str | None = None
    account_number: str | None = None
    currency: str = "RUB"
    is_default: bool = False


class BankAccountUpdate(BaseModel):
    name: str | None = None
    bank_name: str | None = None
    account_number: str | None = None
    is_default: bool | None = None
    is_active: bool | None = None


class BankAccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    organization_id: int
    name: str
    bank_name: str | None
    account_number: str | None
    currency: str
    is_default: bool
    is_active: bool


class OrganizationCreate(BaseModel):
    name: str
    short_name: str
    inn: str | None = None


class OrganizationUpdate(BaseModel):
    name: str | None = None
    short_name: str | None = None
    inn: str | None = None
    is_active: bool | None = None


class OrganizationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    short_name: str
    inn: str | None
    is_active: bool
    accounts: list[BankAccountOut] = []


class MoneyTotals(BaseModel):
    """Приход, расход и сальдо за период. `balance` = income − expense."""

    income: float = 0
    expense: float = 0
    balance: float = 0
    count: int = 0


class AccountSummary(MoneyTotals):
    account_id: int
    name: str


class OrganizationSummary(MoneyTotals):
    organization_id: int
    name: str
    short_name: str
    accounts: list[AccountSummary] = []


class MoneySummaryOut(BaseModel):
    """`GET /money-summary` (0081-a): итог по обеим организациям, по каждой
    организации и по каждому её счёту."""

    total: MoneyTotals
    organizations: list[OrganizationSummary] = []



# --- Единый справочник контрагентов (0081-c) ---


class CounterpartyCreate(BaseModel):
    name: str
    inn: str | None = None
    kind: CounterpartyKind = CounterpartyKind.OTHER
    client_id: int | None = None
    supplier_id: int | None = None
    comment: str | None = None


class CounterpartyUpdate(BaseModel):
    name: str | None = None
    inn: str | None = None
    kind: CounterpartyKind | None = None
    client_id: int | None = None
    supplier_id: int | None = None
    comment: str | None = None
    is_active: bool | None = None


class CounterpartyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    inn: str | None
    kind: CounterpartyKind
    client_id: int | None
    supplier_id: int | None
    comment: str | None
    is_active: bool
    # Агрегаты по проведённым платежам — заполняет service.counterparty_out().
    total_income: float = 0
    total_expense: float = 0
    payments_count: int = 0
    last_payment_at: datetime | None = None


class MoneyMovementCreate(BaseModel):
    subkind: MoneySubkind
    amount: float
    counterparty_id: int | None = None
    # Счёт, по которому прошёл платёж (0081-a). Обязателен для проводки,
    # заводимой человеком; None оставлен для авто-проводок внутри бэка
    # (record_sale_income / pay_supplier_order) — им счёт подставляет
    # service._resolve_account (счёт по умолчанию).
    account_id: int | None = None
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
    document_ids: list[int] = []
    link: str | None = None

    @field_validator("link")
    @classmethod
    def _link_looks_like_url(cls, value: str | None) -> str | None:
        return _validate_link(value)


class MoneyMovementUpdate(BaseModel):
    subkind: MoneySubkind | None = None
    counterparty_id: int | None = None
    account_id: int | None = None
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
    document_ids: list[int] | None = None
    link: str | None = None

    @field_validator("link")
    @classmethod
    def _link_looks_like_url(cls, value: str | None) -> str | None:
        return _validate_link(value)


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
    account_id: int | None
    account_name: str | None = None
    organization_id: int | None = None
    organization_name: str | None = None
    status: MoneyMovementStatus
    posted_at: datetime | None
    doc_date: datetime | None
    cancel_reason: str | None
    payment_purpose: str | None
    comment: str | None
    external_number: str | None
    counterparty_id: int | None
    counterparty_name: str | None = None
    source_kind: MoneySourceKind
    client_id: int | None
    employee_id: int | None
    supply_id: int | None
    source_label: str | None = None
    documents: list[FileAssetOut] = []
    link: str | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_movement(cls, mm: MoneyMovement) -> "MoneyMovementOut":
        out = cls.model_validate(mm)
        out.initiator_name = mm.initiator.full_name if mm.initiator else None
        out.counterparty_name = mm.counterparty.name if mm.counterparty else None
        if mm.account is not None:
            out.account_name = mm.account.name
            out.organization_id = mm.account.organization_id
            out.organization_name = (
                mm.account.organization.short_name if mm.account.organization else None
            )
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


class EmployeeSalaryOverview(BaseModel):
    """Строка раздела «Сотрудники» (0023): сотрудник + его текущая незакрытая
    (draft/approved) зарплатная проводка, если есть. Отдельный эндпоинт, а не
    `GET /api/auth/users`, потому что тот доступен только админу — здесь
    доступ по `Module.ACCOUNTING`, как у остальной бухгалтерии.

    `last_posted_at`/`last_posted_amount` и `kpi` — карточка сотрудника (0041)."""

    employee_id: int
    full_name: str
    open_movement: MoneyMovementOut | None
    # Последняя ПРОВЕДЁННАЯ (posted) зарплатная проводка этого сотрудника —
    # не текущая открытая, а история; None, если проведённых ещё не было.
    last_posted_at: datetime | None = None
    last_posted_amount: float | None = None
    # KPI за текущий календарный месяц (0042: по задачам с прошедшим
    # дедлайном — доля выполненных в срок). None — за месяц нет ни одной
    # оценённой задачи, это не то же самое, что 0.
    kpi: int | None = None


class EmployeeKpiPeriod(BaseModel):
    """Один сохранённый период KPI сотрудника — `GET
    /employee-kpi-history/{employee_id}` (0042). Текущий месяц пересчитан на
    момент запроса; прошлые — замороженный снимок."""

    model_config = ConfigDict(from_attributes=True)

    period_start: date
    period_end: date
    tasks_total: int
    tasks_on_time: int
    tasks_late: int
    tasks_overdue: int
    kpi: int | None


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
    # строк, где колонка контрагента оказалась пустой — привязывать не к чему
    unmatched_source: int = 0
    # строк, совпавших с уже загруженной проводкой этого счёта (0081-e)
    duplicates: int = 0
    counterparties_created: int = 0
    counterparties_matched: int = 0
    # счёт, на который легли проводки
    account_id: int | None = None
    account_label: str | None = None
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
