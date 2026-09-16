import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class ClientStage(str, enum.Enum):
    LEAD = "lead"
    DISCUSSION = "discussion"
    APPROVAL = "approval"
    PAYMENT = "payment"
    POSTPAYMENT = "postpayment"


class ClientChatState(str, enum.Enum):
    """Состояние переписки с клиентом в привязанном чате MAX. Осмысленно
    только при привязанном `max_chat_id` — см. Client.max_chat_state."""

    AGREEMENT = "agreement"
    WAITING = "waiting"
    ANALYSIS = "analysis"


class OrderType(str, enum.Enum):
    """Одиночный заказ — один дом в производстве. Множественный — несколько
    домов у одного клиента, под каждый на стадии производства заводится
    отдельный проект (app.production.models.Production)."""

    SINGLE = "single"
    MULTIPLE = "multiple"


class PaymentPlan(str, enum.Enum):
    """Как клиент рассчитывается — определяет, в какой момент цикла нужны
    деньги, чтобы двигаться дальше:

    - FULL_PREPAYMENT — полная предоплата: весь платёж на стадии «оплата»,
      до старта производства (историческое поведение; все ранее заведённые
      клиенты имеют этот план).
    - ADVANCE_THEN_BALANCE — аванс + оплата после получения: на стадии
      «оплата» вносится аванс (`advance_amount`), производство стартует;
      остаток гасится после получения дома — до завершения цикла.
    - POST_PAYMENT — оплата после получения: на стадии «оплата» деньги не
      требуются, производство стартует сразу; вся сумма гасится после
      получения дома — до завершения цикла.
    """

    FULL_PREPAYMENT = "full_prepayment"
    ADVANCE_THEN_BALANCE = "advance_then_balance"
    POST_PAYMENT = "post_payment"


PAYMENT_PLAN_LABELS: dict[str, PaymentPlan] = {
    "полная предоплата": PaymentPlan.FULL_PREPAYMENT,
    "аванс + оплата после получения": PaymentPlan.ADVANCE_THEN_BALANCE,
    "аванс и оплата после получения": PaymentPlan.ADVANCE_THEN_BALANCE,
    "оплата после получения": PaymentPlan.POST_PAYMENT,
}


def parse_payment_plan(value: str) -> PaymentPlan:
    """Принимает `payment_plan` и как значение enum (`full_prepayment`…), и как
    русский лейбл («Полная предоплата»…) — Марина иногда передаёт то, что
    сказал человек, а не точное значение enum. Неизвестное значение —
    `ValueError` с перечислением допустимых (вызывающий код решает, во что это
    завернуть)."""
    try:
        return PaymentPlan(value)
    except ValueError:
        pass
    plan = PAYMENT_PLAN_LABELS.get(value.strip().lower())
    if plan is not None:
        return plan
    allowed = ", ".join(f"'{p.value}'" for p in PaymentPlan)
    labels = ", ".join(f"«{label.capitalize()}»" for label in (
        "полная предоплата", "аванс + оплата после получения", "оплата после получения"
    ))
    raise ValueError(f"Неизвестный формат расчёта: {value!r}. Допустимые значения: {allowed} ({labels}).")


CLIENT_STAGE_ORDER = [
    ClientStage.LEAD,
    ClientStage.DISCUSSION,
    ClientStage.APPROVAL,
    ClientStage.PAYMENT,
    ClientStage.POSTPAYMENT,
]


class Client(Base):
    __tablename__ = "clients"
    __table_args__ = (UniqueConstraint("max_chat_id", name="uq_clients_max_chat_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    cycle_id: Mapped[int] = mapped_column(ForeignKey("cycles.id"), unique=True, nullable=False)
    stage: Mapped[ClientStage] = mapped_column(
        Enum(ClientStage, name="client_stage"), nullable=False, default=ClientStage.LEAD
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # --- Base info: required at creation, immutable forever after ---
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[str] = mapped_column(String(32), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    # Список способов связи: [{"messenger": "telegram", "contact": "@ivan"}, ...].
    # Паспорт/ИНН/дата рождения больше не собираются — для работы с клиентом
    # достаточно знать, где и как с ним связаться.
    contacts: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # ID чата в мессенджере MAX (app/max), к которому привязана переписка с
    # клиентом. Редактируется в любой момент, ни к одной стадии не привязан.
    # 0 — «Избранное» (чат с самим собой); id групп/каналов бывают
    # отрицательными и большими, поэтому BigInteger.
    max_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Состояние переписки — хранится на связи, а не на клиенте: сбрасывается
    # при отвязке чата (см. client_service.set_max_chat_id).
    max_chat_state: Mapped["ClientChatState | None"] = mapped_column(
        Enum(ClientChatState, name="client_chat_state"), nullable=True
    )

    # --- Documents info: appears at APPROVAL, required before PAYMENT, then locked ---
    # order_type/house_model_key used to live in a separate "project" group at
    # DISCUSSION (with wishes/area/price/layout free-text fields) — removed by
    # 0044, replaced by picking a real catalog model (app.house_models). Only
    # order_type is required to leave APPROVAL (needed below); house_model_key
    # is an optional reference — not every real house matches a catalog card.
    order_type: Mapped[OrderType | None] = mapped_column(Enum(OrderType, name="order_type"), nullable=True)
    house_model_key: Mapped[str | None] = mapped_column(ForeignKey("house_model_cards.key"), nullable=True)
    # houses_count is meaningful only for a MULTIPLE order; a SINGLE order is
    # forced to 1. It fixes how many Production projects are spun up on
    # POSTPAYMENT. Unlike the rest of this group, it's never locked (0044) —
    # editable any time, updated through its own endpoint/service function.
    houses_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    final_price: Mapped[float | None] = mapped_column(Numeric(14, 2, asdecimal=False), nullable=True)
    # Формат расчёта. Появляется на «согласовании», фиксируется вместе с
    # остальными документными данными. Для всех ранее заведённых клиентов —
    # полная предоплата (server_default). advance_amount обязателен и осмыслен
    # только при ADVANCE_THEN_BALANCE.
    payment_plan: Mapped[PaymentPlan] = mapped_column(
        Enum(PaymentPlan, name="payment_plan"),
        nullable=False,
        default=PaymentPlan.FULL_PREPAYMENT,
        server_default=PaymentPlan.FULL_PREPAYMENT.name,
    )
    advance_amount: Mapped[float | None] = mapped_column(Numeric(14, 2, asdecimal=False), nullable=True)
    installation_address: Mapped[str | None] = mapped_column(String(500), nullable=True)
    contract_file_id: Mapped[int | None] = mapped_column(ForeignKey("file_assets.id"), nullable=True)
    # Приложение к договору грузится вместе с самим договором одним действием
    # (0061) — see client_service.set_contract_files. Не бывает одного без
    # другого: оба обязательны для ухода со стадии APPROVAL.
    contract_appendix_file_id: Mapped[int | None] = mapped_column(ForeignKey("file_assets.id"), nullable=True)
    # house_project — опционален с 0061 (не у каждого клиента есть в системе);
    # не входит в _DOCUMENTS_REQUIRED. АР/КР — обязательны с 0061.
    house_project_file_id: Mapped[int | None] = mapped_column(ForeignKey("file_assets.id"), nullable=True)
    ar_file_id: Mapped[int | None] = mapped_column(ForeignKey("file_assets.id"), nullable=True)
    kr_file_id: Mapped[int | None] = mapped_column(ForeignKey("file_assets.id"), nullable=True)
    documents_locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # --- Payment: appears at PAYMENT, required before POSTPAYMENT, then locked ---
    # `is_paid` — поступил ли платёж, нужный ДЛЯ СТАРТА производства:
    #   FULL_PREPAYMENT — вся сумма; ADVANCE_THEN_BALANCE — аванс;
    #   POST_PAYMENT — не требуется (переход возможен при is_paid = False).
    is_paid: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    payment_locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # `balance_paid` — погашен ли остаток «после получения». Для FULL_PREPAYMENT
    # проставляется автоматически на стадии «оплата». Для остальных планов —
    # вручную на «постоплате» после получения дома; пока не True, цикл нельзя
    # перевести в COMPLETED (см. app.installation.service.complete_installation).
    balance_paid: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    balance_paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    cycle: Mapped["Cycle"] = relationship(back_populates="client")  # noqa: F821
    notes: Mapped[list["ClientNote"]] = relationship(back_populates="client", cascade="all, delete-orphan")
    contract_file: Mapped["FileAsset"] = relationship(foreign_keys=[contract_file_id])  # noqa: F821
    contract_appendix_file: Mapped["FileAsset"] = relationship(foreign_keys=[contract_appendix_file_id])  # noqa: F821
    house_project_file: Mapped["FileAsset"] = relationship(foreign_keys=[house_project_file_id])  # noqa: F821
    ar_file: Mapped["FileAsset"] = relationship(foreign_keys=[ar_file_id])  # noqa: F821
    kr_file: Mapped["FileAsset"] = relationship(foreign_keys=[kr_file_id])  # noqa: F821
    # Read-only reference into the house_models catalog (0043) — this section
    # doesn't own or manage that data, just points at it.
    house_model: Mapped["HouseModelCard | None"] = relationship(viewonly=True)  # noqa: F821


class ClientNote(Base):
    __tablename__ = "client_notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), nullable=False)
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    client: Mapped["Client"] = relationship(back_populates="notes")
    author: Mapped["User"] = relationship()  # noqa: F821
