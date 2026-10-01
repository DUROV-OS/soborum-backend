"""Раздел «Бухгалтерия» — единый реестр движения денежных средств.

Модель и терминология повторяют МойСклад (см.
`backend/docs/moysklad-accounting-research.md`, разведка задачи 0011-b):

- вид проводки = направление (`paymentin`/`cashin` против `paymentout`/`cashout`)
  плюс подвид из статьи ДДС МойСклад (`expenseItem`: «Зарплата», «Закупка
  товаров», «Налоги и сборы», «Аренда»…);
- `status` = наш процессный слой поверх `applicable` МойСклад: `posted` ⇔
  «проведён» (деньги и взаиморасчёты учтены, запись неизменяема);
- `source_ref` — полиморфная привязка ровно к одному источнику (клиент /
  сотрудник / поставка), как `agent` + `operations` у денежного документа.

Сшивку с «Состоянием оплаты» клиента, зарплатой сотрудника и оплатой поставки
эта модель НЕ делает — это задача 0011-f. Здесь только хранение и статусная
машина.
"""

import enum
from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

# Документы, прикреплённые к проводке (0072-d) — тот же приём, что и
# task_images (app/tasks/models.py): M2M со общим хранилищем FileAsset.
money_movement_documents = Table(
    "money_movement_documents",
    Base.metadata,
    Column("money_movement_id", ForeignKey("money_movements.id", ondelete="CASCADE"), primary_key=True),
    Column("file_id", ForeignKey("file_assets.id", ondelete="CASCADE"), primary_key=True),
)


class MoneyDirection(str, enum.Enum):
    INCOME = "income"
    EXPENSE = "expense"


class MoneySubkind(str, enum.Enum):
    """Подвид проводки. Ярлыки — из встроенных статей ДДС МойСклад; набор
    фиксированный (настраиваемый справочник статей в MVP не заводим)."""

    SALE_INCOME = "sale_income"  # доход от продажи (paymentin + счёт/отгрузка клиента)
    SALARY_PAYOUT = "salary_payout"  # выплата зарплаты (paymentout, статья «Зарплата»)
    SUPPLY_PAYMENT = "supply_payment"  # оплата поставки (paymentout, статья «Закупка товаров»)
    TAX = "tax"  # налоги и сборы
    RENT = "rent"  # аренда
    OTHER_INCOME = "other_income"  # прочий доход
    OTHER_EXPENSE = "other_expense"  # прочий расход


class MoneyAssessment(str, enum.Enum):
    """Оценка. В МойСклад у денежного документа «шкалы достоверности» нет;
    ближайшее — разделение планового обязательства и фактического платежа."""

    PLANNED = "planned"
    ACTUAL = "actual"


class MoneyMovementStatus(str, enum.Enum):
    DRAFT = "draft"  # черновик (applicable=false)
    APPROVED = "approved"  # согласовано (наш слой; в МойСклад — state)
    POSTED = "posted"  # проведено (applicable=true); неизменяемо
    CANCELLED = "cancelled"  # отменено с основанием


class MoneySourceKind(str, enum.Enum):
    NONE = "none"
    CLIENT = "client"
    EMPLOYEE = "employee"
    SUPPLY = "supply"


# Направление, подразумеваемое подвидом.
INCOME_SUBKINDS: set[MoneySubkind] = {MoneySubkind.SALE_INCOME, MoneySubkind.OTHER_INCOME}
EXPENSE_SUBKINDS: set[MoneySubkind] = {
    MoneySubkind.SALARY_PAYOUT,
    MoneySubkind.SUPPLY_PAYMENT,
    MoneySubkind.TAX,
    MoneySubkind.RENT,
    MoneySubkind.OTHER_EXPENSE,
}

# Подвид -> обязательный источник. Подвиды не в словаре привязку не допускают.
SUBKIND_REQUIRED_SOURCE: dict[MoneySubkind, MoneySourceKind] = {
    MoneySubkind.SALE_INCOME: MoneySourceKind.CLIENT,
    MoneySubkind.SALARY_PAYOUT: MoneySourceKind.EMPLOYEE,
    MoneySubkind.SUPPLY_PAYMENT: MoneySourceKind.SUPPLY,
}

# Русский лейбл подвида — для заголовков задач на согласование (0011-f).
MONEY_SUBKIND_LABELS: dict[MoneySubkind, str] = {
    MoneySubkind.SALE_INCOME: "доход от продажи",
    MoneySubkind.SALARY_PAYOUT: "выплата зарплаты",
    MoneySubkind.SUPPLY_PAYMENT: "оплата поставки",
    MoneySubkind.TAX: "налоги и сборы",
    MoneySubkind.RENT: "аренда",
    MoneySubkind.OTHER_INCOME: "прочий доход",
    MoneySubkind.OTHER_EXPENSE: "прочий расход",
}


class SupplierOrderStatus(str, enum.Enum):
    """Статус физического исполнения заказа. Только вперёд, без пропуска шага
    (валидируется в service). Независим от оплаты — proведение
    `MoneyMovement(supply_payment)` не меняет этот статус и наоборот (0011-f)."""

    ORDERED = "ordered"
    IN_TRANSIT = "in_transit"
    RECEIVED = "received"


class SupplierOrder(Base):
    """Заказ у поставщика (`app.warehouse.Supplier`): материалы, стоимость,
    срок, статус исполнения. Источник для `MoneyMovement.supply_id`
    (подвид `supply_payment`) — задача 0011-d.

    Не путать с `app.warehouse.Supply` — это отдельная, более старая сущность
    «мгновенный приход материалов на склад» (без цены и статусов, привязана
    к `warehouse_materials`), не связанная с этой фичей."""

    __tablename__ = "supplier_orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), nullable=False)
    # позиции заказа: [{"material", "category", "quantity", "unit_price"}] —
    # цена на момент заказа, а не диапазон, как в SupplierPriceItem
    items: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # сумма по items, считается сервисом при создании/правке — не приходит от клиента
    total_cost: Mapped[float] = mapped_column(Numeric(14, 2, asdecimal=False), nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="RUB", server_default="RUB")
    expected_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[SupplierOrderStatus] = mapped_column(
        Enum(SupplierOrderStatus, name="supplier_order_status"),
        nullable=False,
        default=SupplierOrderStatus.ORDERED,
        server_default=SupplierOrderStatus.ORDERED.name,
    )
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    supplier: Mapped["Supplier"] = relationship()  # noqa: F821


class Organization(Base):
    """Юрлицо компании. Деньги компания ведёт через два ООО — «ИД Групп» и
    «Технология» (0081); счёт всегда принадлежит одной организации, проводка —
    одному счёту, поэтому организация — верхний уровень разделения денег в
    разделе.

    Не демо-данные: без организаций и счетов реестр не работает вовсе, поэтому
    стартовый набор заводится во всех окружениях (см. `accounting/seed.py` →
    `ensure_organizations_seed`), а не под `ENABLE_DEMO_SEED`."""

    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # подпись вкладки в интерфейсе — «ИД Групп», а не «ООО «ИД Групп»»
    short_name: Mapped[str] = mapped_column(String(64), nullable=False)
    inn: Mapped[str | None] = mapped_column(String(16), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    accounts: Mapped[list["BankAccount"]] = relationship(
        back_populates="organization", order_by="BankAccount.id"
    )


class BankAccount(Base):
    """Банковский счёт организации. Один счёт организации помечен
    `is_default` — на него попадают проводки, созданные без явного выбора
    счёта (авто-проводки 0011-f) и на нём открывается вкладка организации."""

    __tablename__ = "bank_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    bank_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # строкой, а не числом: расчётный счёт — 20 цифр с ведущими нулями
    account_number: Mapped[str | None] = mapped_column(String(34), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="RUB", server_default="RUB")
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    organization: Mapped["Organization"] = relationship(back_populates="accounts")


class CounterpartyKind(str, enum.Enum):
    """Тип контрагента. Клиент и поставщик дублируют существующие реестры
    (`clients`, `suppliers`) ссылкой, а не копией данных; `government` и
    `other` — то, чего в системе нет вовсе (налоговая, арендодатель, разовый
    подрядчик) и из-за чего платёж раньше оставался без привязки."""

    CLIENT = "client"
    SUPPLIER = "supplier"
    EMPLOYEE = "employee"
    GOVERNMENT = "government"
    OTHER = "other"


class Counterparty(Base):
    """Единый справочник контрагентов (0081-c).

    Не отменяет `MoneyMovement.source_kind`: та привязка операционная (на ней
    держатся оплата поставки и состояние оплаты клиента, 0011-f), эта —
    денежная и общая, существует для любого платежа, включая налоги и аренду.
    Поэтому у проводки могут быть заполнены обе."""

    __tablename__ = "counterparties"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # ИНН уникален среди непустых; NULL-ы Postgres в UNIQUE не сравнивает,
    # так что контрагентов без ИНН может быть сколько угодно.
    inn: Mapped[str | None] = mapped_column(String(16), nullable=True)
    kind: Mapped[CounterpartyKind] = mapped_column(
        Enum(CounterpartyKind, name="counterparty_kind"),
        nullable=False,
        default=CounterpartyKind.OTHER,
        server_default=CounterpartyKind.OTHER.name,
    )
    # Заполнено не более одного: контрагент — либо наш клиент, либо поставщик,
    # либо никто из известных системе (валидируется в service).
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id"), nullable=True)
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id"), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    client: Mapped["Client"] = relationship(foreign_keys=[client_id])  # noqa: F821
    supplier: Mapped["Supplier"] = relationship(foreign_keys=[supplier_id])  # noqa: F821


class MoneyMovement(Base):
    __tablename__ = "money_movements"

    id: Mapped[int] = mapped_column(primary_key=True)

    direction: Mapped[MoneyDirection] = mapped_column(
        Enum(MoneyDirection, name="money_direction"), nullable=False
    )
    subkind: Mapped[MoneySubkind] = mapped_column(
        Enum(MoneySubkind, name="money_subkind"), nullable=False
    )

    # Суммы — в рублях, как везде в бэке. МойСклад отдаёт копейки (×100) — учесть
    # при будущем обмене.
    amount: Mapped[float] = mapped_column(Numeric(14, 2, asdecimal=False), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="RUB", server_default="RUB")
    # Сумма налога (← vatSum МойСклад). Ставку (%) в MVP не храним.
    tax: Mapped[float] = mapped_column(
        Numeric(14, 2, asdecimal=False), nullable=False, default=0, server_default="0"
    )

    assessment: Mapped[MoneyAssessment] = mapped_column(
        Enum(MoneyAssessment, name="money_assessment"),
        nullable=False,
        default=MoneyAssessment.ACTUAL,
        server_default=MoneyAssessment.ACTUAL.name,
    )
    # Аналог expenseitem.operatingExpenses — учитывать ли в оценке прибыли.
    affects_profit: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    # Инициатор — сотрудник, заведший проводку (← owner денежного документа).
    initiator_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)

    status: Mapped[MoneyMovementStatus] = mapped_column(
        Enum(MoneyMovementStatus, name="money_movement_status"),
        nullable=False,
        default=MoneyMovementStatus.DRAFT,
        server_default=MoneyMovementStatus.DRAFT.name,
    )
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Дата платёжного документа (← moment МойСклад). Заполняется при импорте
    # выпиской (0011-k); при ручном создании пусто. Реестр и период-фильтр
    # используют её перед posted_at / created_at. Держим DateTime (а не Date)
    # для однотипного coalesce со служебными датами.
    doc_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    payment_purpose: Mapped[str | None] = mapped_column(String(500), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Задел под банк-интеграцию (← incomingNumber МойСклад).
    external_number: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Произвольная ссылка на проводку (0072-d) — одна, не список.
    link: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Счёт, по которому прошёл платёж (0081). В БД nullable — иначе не
    # мигрировать проводки, заведённые до появления счетов; миграция проставляет
    # им счёт по умолчанию первой организации. На входе API обязателен:
    # `POST /money-movements` без счёта — 422 (service._resolve_account).
    account_id: Mapped[int | None] = mapped_column(ForeignKey("bank_accounts.id"), nullable=True)

    # Контрагент из единого справочника (0081-c). Отдельно от source_kind:
    # та привязка операционная и есть не у каждого платежа, эта — денежная и
    # общая (налоговая, арендодатель, разовый подрядчик тоже контрагенты).
    counterparty_id: Mapped[int | None] = mapped_column(ForeignKey("counterparties.id"), nullable=True)

    # Полиморфная привязка: заполнено не более одного из client/employee/supply,
    # source_kind согласован с тем, что заполнено (валидируется в service).
    source_kind: Mapped[MoneySourceKind] = mapped_column(
        Enum(MoneySourceKind, name="money_source_kind"),
        nullable=False,
        default=MoneySourceKind.NONE,
        server_default=MoneySourceKind.NONE.name,
    )
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id"), nullable=True)
    employee_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    # ссылается на SupplierOrder (эта же модель, задача 0011-d), а не на
    # app.warehouse.Supply — см. докстринг SupplierOrder
    supply_id: Mapped[int | None] = mapped_column(ForeignKey("supplier_orders.id"), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    initiator: Mapped["User"] = relationship(foreign_keys=[initiator_id])  # noqa: F821
    client: Mapped["Client"] = relationship(foreign_keys=[client_id])  # noqa: F821
    employee: Mapped["User"] = relationship(foreign_keys=[employee_id])  # noqa: F821
    documents: Mapped[list["FileAsset"]] = relationship(secondary=money_movement_documents)  # noqa: F821
    supply: Mapped["SupplierOrder"] = relationship(foreign_keys=[supply_id])
    account: Mapped["BankAccount"] = relationship(foreign_keys=[account_id])
    counterparty: Mapped["Counterparty"] = relationship(foreign_keys=[counterparty_id])


class EmployeeKpi(Base):
    """Снимок KPI сотрудника за календарный месяц (задача 0042, заменяет
    случайную заглушку из 0041).

    Формула (см. `backlog/DONE/0042-employee-kpi-calculation.md` → «Решение по
    открытым вопросам»): доля задач (`app.tasks.Task`), где сотрудник — среди
    `assignees`, с `deadline` в этом месяце и уже прошедшим, выполненных в
    срок (вес 1) или с опозданием (вес 0.5); просроченные незакрытые — 0.
    Единственный источник — `tasks`, единственный раздел, синхронизированный
    со всеми остальными и одинаково применимый к любой роли.

    Текущий (незакрытый) месяц пересчитывается и перезаписывается при каждом
    обращении к `salary-overview`; прошлые периоды — замороженный снимок, не
    пересчитываются («история не переписывается», `docs/PROJECT.md`)."""

    __tablename__ = "employee_kpis"
    __table_args__ = (
        UniqueConstraint("employee_id", "period_start", name="uq_employee_kpi_period"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)

    tasks_total: Mapped[int] = mapped_column(nullable=False, default=0)
    tasks_on_time: Mapped[int] = mapped_column(nullable=False, default=0)
    tasks_late: Mapped[int] = mapped_column(nullable=False, default=0)
    tasks_overdue: Mapped[int] = mapped_column(nullable=False, default=0)
    # null — за период не нашлось ни одной оценённой задачи (не 0: отсутствие
    # данных не равно провалу по KPI).
    kpi: Mapped[int | None] = mapped_column(nullable=True)

    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    employee: Mapped["User"] = relationship(foreign_keys=[employee_id])  # noqa: F821
