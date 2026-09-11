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
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


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
    cancel_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    payment_purpose: Mapped[str | None] = mapped_column(String(500), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Задел под банк-интеграцию (← incomingNumber МойСклад).
    external_number: Mapped[str | None] = mapped_column(String(255), nullable=True)

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
    supply_id: Mapped[int | None] = mapped_column(ForeignKey("supplies.id"), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    initiator: Mapped["User"] = relationship(foreign_keys=[initiator_id])  # noqa: F821
    client: Mapped["Client"] = relationship(foreign_keys=[client_id])  # noqa: F821
    employee: Mapped["User"] = relationship(foreign_keys=[employee_id])  # noqa: F821
    supply: Mapped["Supply"] = relationship(foreign_keys=[supply_id])  # noqa: F821
