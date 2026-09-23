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
    """Путь клиента от первого обращения до принятого дома (0079).

    Значения первых пяти стадий остались от пятиколоночного пути — поменялись
    только человеческие подписи (см. STAGE_LABELS):
    `approval` — «Ипотека/Одобрение в банке», `payment` — «Договор подписан/
    Аванс внесён», `postpayment` — «Дом в производстве».
    """

    LEAD = "lead"
    DISCUSSION = "discussion"
    SITE_VISIT = "site_visit"
    APPROVAL = "approval"
    PAYMENT = "payment"
    POSTPAYMENT = "postpayment"
    ACCEPTANCE = "acceptance"
    COMPLETED = "completed"


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
    ClientStage.SITE_VISIT,
    ClientStage.APPROVAL,
    ClientStage.PAYMENT,
    ClientStage.POSTPAYMENT,
    ClientStage.ACCEPTANCE,
    ClientStage.COMPLETED,
]

STAGE_LABELS: dict[ClientStage, str] = {
    ClientStage.LEAD: "Лид",
    ClientStage.DISCUSSION: "Обсуждение",
    ClientStage.SITE_VISIT: "Гость на объекте",
    ClientStage.APPROVAL: "Ипотека/Одобрение в банке",
    ClientStage.PAYMENT: "Договор подписан/Аванс внесён",
    ClientStage.POSTPAYMENT: "Дом в производстве",
    ClientStage.ACCEPTANCE: "Приёмка",
    ClientStage.COMPLETED: "Успешно реализовано",
}

# Стадии, с которых клиента двигает человек кнопкой «следующая стадия».
# Всё, что после «Дом в производстве», двигается автоматически по монтажу
# (app.installation.service) — руками такие стадии не переводят, и задача
# «перевести на следующую стадию» на них не заводится.
MANUAL_TRANSITION_STAGES = [
    ClientStage.LEAD,
    ClientStage.DISCUSSION,
    ClientStage.SITE_VISIT,
    ClientStage.APPROVAL,
    ClientStage.PAYMENT,
]


def stage_label(stage: ClientStage) -> str:
    return STAGE_LABELS[stage]


def stages_after(stage: ClientStage) -> list[ClientStage]:
    """Стадии строго позже указанной — чтобы проверки «клиент уже прошёл X»
    не перечисляли стадии руками и не отставали при добавлении новых."""
    return CLIENT_STAGE_ORDER[CLIENT_STAGE_ORDER.index(stage) + 1 :]


class Client(Base):
    __tablename__ = "clients"

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
    # Явное разрешение администратора обходить payment_locked_at (0054). Не
    # трогает сам факт блокировки — только снимает запрет на редактирование,
    # пока включено. Переключается через отдельный admin-only эндпоинт.
    payment_edit_unlocked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    # `balance_paid` — погашен ли остаток «после получения». Для FULL_PREPAYMENT
    # проставляется автоматически на стадии «оплата». Для остальных планов —
    # вручную на «постоплате» после получения дома; пока не True, цикл нельзя
    # перевести в COMPLETED (см. app.installation.service.complete_installation).
    balance_paid: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    balance_paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    cycle: Mapped["Cycle"] = relationship(back_populates="client")  # noqa: F821
    notes: Mapped[list["ClientNote"]] = relationship(back_populates="client", cascade="all, delete-orphan")
    chat_links: Mapped[list["ClientChatLink"]] = relationship(
        back_populates="client", cascade="all, delete-orphan", order_by="ClientChatLink.id"
    )
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


class ClientChatLink(Base):
    """Привязка клиента к чату MAX — один-ко-многим со стороны клиента (0053):
    у клиента может быть несколько чатов (например, отдельно с ним и с его
    помощником), но каждый чат по-прежнему принадлежит не более чем одному
    клиенту (``max_chat_id`` уникален глобально, как раньше на ``Client``).
    Заменяет бывшие `Client.max_chat_id`/`Client.max_chat_state` (0012)."""

    __tablename__ = "client_chat_links"
    __table_args__ = (UniqueConstraint("max_chat_id", name="uq_client_chat_links_max_chat_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), nullable=False)
    # 0 — «Избранное» (чат с самим собой); id групп/каналов бывают
    # отрицательными и большими, поэтому BigInteger.
    max_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Например «С клиентом», «С помощником» — различает несколько чатов одного клиента в UI.
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    state: Mapped["ClientChatState | None"] = mapped_column(
        Enum(ClientChatState, name="client_chat_state"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    client: Mapped["Client"] = relationship(back_populates="chat_links")
