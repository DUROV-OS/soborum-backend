"""Уведомления (0080-b): хранилище событий-уведомлений и настроек мьюта.

Это только хранилище и API — генерация событий (0080-c, кто и когда создаёт
`Notification` для каждого повода) и push-доставка (0080-e) — отдельные
задачи. Форма по образцу ленты заявок `app/feedback/models.py`
(FeedbackRequest/FeedbackEvent): плоский лог + отметка «прочитано»."""

import enum
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.common.module_access import Module
from app.db.base import Base


class NotificationKind(str, enum.Enum):
    """Повод уведомления. Генерацию под каждый повод добавляет 0080-c —
    здесь только перечисление того, что бывает."""

    TASK_DUE = "task_due"
    TASK_OVERDUE = "task_overdue"
    CLIENT_UPDATE = "client_update"
    PRODUCTION_UPDATE = "production_update"
    INSTALLATION_UPDATE = "installation_update"
    MATERIAL_REQUEST_UPDATE = "material_request_update"
    MONEY_MOVEMENT_UPDATE = "money_movement_update"
    CONTENT_UPDATE = "content_update"
    MAX_MESSAGE = "max_message"
    FEEDBACK_UPDATE = "feedback_update"


class Notification(Base):
    """Одно уведомление получателю. Не редактируется после создания — только
    `read_at` при отметке прочитанным."""

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    kind: Mapped[NotificationKind] = mapped_column(Enum(NotificationKind, name="notification_kind"), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Ссылка на объект-повод для перехода из карточки уведомления на фронте
    # (0080-d) — слаг раздела фронта + id объекта в нём, свободной формы,
    # как `FeedbackRequest.section`: типов объектов больше, чем разделов
    # доступа (`Module`), заводить под это отдельный enum незачем.
    object_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    object_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped["User"] = relationship()  # noqa: F821


class PushSubscription(Base):
    """Браузерная push-подписка (0080-e): одна строка на пару
    браузер/устройство. Несколько подписок на одного пользователя — это
    нормально (разные браузеры/устройства), ключ уникальности — `endpoint`
    (его отдаёт сам браузер, он один на связку browser+origin), а не
    `user_id` — иначе повторная подписка того же пользователя с другого
    устройства затирала бы первую."""

    __tablename__ = "push_subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    endpoint: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    # Ключи шифрования payload (Web Push: ECDH p256dh + auth secret),
    # ровно то, что отдаёт `PushSubscription.toJSON().keys` в браузере.
    p256dh: Mapped[str] = mapped_column(String(255), nullable=False)
    auth: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped["User"] = relationship()  # noqa: F821


class NotificationMute(Base):
    """Настройка «не уведомлять»: весь раздел (`object_id is NULL`) или
    конкретный объект в разделе. `module` — тот же `Module`, что в матрице
    доступа (`app/common/module_access.py`), не отдельный список разделов."""

    __tablename__ = "notification_mutes"
    __table_args__ = (UniqueConstraint("user_id", "module", "object_id", name="uq_notification_mute"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    module: Mapped[Module] = mapped_column(Enum(Module, name="module"), nullable=False)
    object_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped["User"] = relationship()  # noqa: F821
