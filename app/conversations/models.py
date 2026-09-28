"""Переписка из карточки клиента/партнёра через мессенджеры (0083-d).

Бот не может написать человеку первым и не ищет его по телефону, поэтому
чат привязывается к карточке по ссылке-приглашению: у каждой пары
(владелец, канал) — свой ``invite_token``, человек открывает ссылку на бота с
этой меткой, бот получает событие — и чат становится каналом карточки.

Лента хранится здесь, одной таблицей на все каналы, а не собирается из
max_bot_messages/телеграма на лету: карточке нужна история всех каналов по
времени, с автором-сотрудником у исходящих и пометкой недоставленных.
"""

import enum
from datetime import datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class ChannelKind(str, enum.Enum):
    MAX = "MAX"
    TELEGRAM = "TELEGRAM"
    WHATSAPP = "WHATSAPP"


class ChannelStatus(str, enum.Enum):
    INVITED = "INVITED"  # ссылка выдана, человек ещё не нажал «Начать»
    CONNECTED = "CONNECTED"  # чат привязан, можно писать
    STOPPED = "STOPPED"  # человек остановил/заблокировал бота


class MessageDirection(str, enum.Enum):
    IN = "IN"
    OUT = "OUT"


class MessageDelivery(str, enum.Enum):
    SENT = "SENT"
    FAILED = "FAILED"


class ContactChannel(Base):
    __tablename__ = "contact_channels"
    __table_args__ = (
        # Ровно один владелец: клиент или партнёр.
        CheckConstraint(
            "(client_id IS NULL) <> (partner_id IS NULL)", name="ck_contact_channels_one_owner"
        ),
        UniqueConstraint("client_id", "channel", name="uq_contact_channels_client_channel"),
        UniqueConstraint("partner_id", "channel", name="uq_contact_channels_partner_channel"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), nullable=True)
    partner_id: Mapped[int | None] = mapped_column(ForeignKey("partners.id", ondelete="CASCADE"), nullable=True)
    channel: Mapped[ChannelKind] = mapped_column(Enum(ChannelKind, name="contact_channel_kind"), nullable=False)
    status: Mapped[ChannelStatus] = mapped_column(
        Enum(ChannelStatus, name="contact_channel_status"), nullable=False, default=ChannelStatus.INVITED
    )
    # Метка в ссылке на бота. Только [a-z0-9_]: набор символов payload в
    # диплинке MAX документацией не оговорён, а у Telegram он ограничен.
    invite_token: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    # chat_id в мессенджере — строкой: у MAX и Telegram это целые, у WhatsApp — номер.
    external_chat_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    external_user_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    messages: Mapped[list["ConversationMessage"]] = relationship(
        back_populates="contact_channel", cascade="all, delete-orphan"
    )


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"
    __table_args__ = (
        # Повтор события (webhook при сбое, polling после рестарта) не дублирует.
        UniqueConstraint("contact_channel_id", "external_message_id", name="uq_conversation_messages_external"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    contact_channel_id: Mapped[int] = mapped_column(
        ForeignKey("contact_channels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    direction: Mapped[MessageDirection] = mapped_column(
        Enum(MessageDirection, name="conversation_message_direction"), nullable=False
    )
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    attachments: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # Сотрудник, отправивший из карточки; у входящих и у исходящих, отправленных
    # мимо карточки (например из «Все чаты»), — пусто.
    author_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    external_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    delivery: Mapped[MessageDelivery] = mapped_column(
        Enum(MessageDelivery, name="conversation_message_delivery"), nullable=False, default=MessageDelivery.SENT
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Время по часам мессенджера; у недоставленных — пусто.
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    contact_channel: Mapped["ContactChannel"] = relationship(back_populates="messages")
