"""Переписка из карточки клиента/партнёра (0083-d)."""

from __future__ import annotations

import enum
import secrets
from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.clients.models import Client
from app.conversations import channels
from app.conversations.models import (
    ChannelKind,
    ChannelStatus,
    ContactChannel,
    ConversationMessage,
    MessageDelivery,
    MessageDirection,
)
from app.partners.models import Partner
from app.users.models import User


class OwnerKind(str, enum.Enum):
    CLIENTS = "clients"
    PARTNERS = "partners"


class ChannelUnavailable(Exception):
    """Канал у этого человека не подключён — роутер отвечает 409 с текстом
    «Пользователь не найден в Макс. Доступные каналы: …» и списком каналов."""

    def __init__(self, message: str, available: list[ChannelKind]):
        super().__init__(message)
        self.message = message
        self.available = available


class ChannelSendFailed(Exception):
    """Мессенджер не принял сообщение — 502; в ленте остаётся строка FAILED."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


# -- владелец ---------------------------------------------------------------

def get_owner_or_404(db: Session, owner: OwnerKind, owner_id: int) -> Client | Partner:
    model = Client if owner == OwnerKind.CLIENTS else Partner
    obj = db.get(model, owner_id)
    if obj is None:
        detail = "Клиент не найден" if owner == OwnerKind.CLIENTS else "Партнёр не найден"
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)
    return obj


def _owner_filter(obj: Client | Partner):
    if isinstance(obj, Client):
        return ContactChannel.client_id == obj.id
    return ContactChannel.partner_id == obj.id


def owner_channels(db: Session, obj: Client | Partner) -> dict[ChannelKind, ContactChannel]:
    rows = db.query(ContactChannel).filter(_owner_filter(obj)).all()
    return {row.channel: row for row in rows}


# -- приглашения ------------------------------------------------------------

def _new_token() -> str:
    # [a-z0-9_] и 24 символа: влезает и в payload диплинка MAX (≤128), и в
    # start-параметр Telegram (≤64, [A-Za-z0-9_-]).
    return "dos_" + secrets.token_hex(10)


def get_or_create_invite(db: Session, obj: Client | Partner, kind: ChannelKind) -> ContactChannel:
    if not channels.is_configured(kind):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Канал «{channels.CHANNEL_LABELS[kind]}» не настроен на сервере",
        )
    existing = owner_channels(db, obj).get(kind)
    if existing is not None:
        return existing
    row = ContactChannel(
        channel=kind,
        status=ChannelStatus.INVITED,
        invite_token=_new_token(),
        client_id=obj.id if isinstance(obj, Client) else None,
        partner_id=obj.id if isinstance(obj, Partner) else None,
    )
    db.add(row)
    db.flush()
    return row


def invite_link(row: ContactChannel) -> str | None:
    adapter = channels.adapter_for(row.channel)
    return adapter.invite_link(row.invite_token) if adapter else None


# -- состояние и лента ------------------------------------------------------

def channel_states(db: Session, obj: Client | Partner) -> list[dict]:
    rows = owner_channels(db, obj)
    states = []
    for kind in ChannelKind:
        row = rows.get(kind)
        states.append(
            {
                "channel": kind,
                "configured": channels.is_configured(kind),
                "status": row.status.value if row else "NONE",
                "invite_link": invite_link(row) if row else None,
                "connected_name": row.external_user_name if row else None,
                "connected_at": row.connected_at if row else None,
            }
        )
    return states


def feed(db: Session, obj: Client | Partner) -> list[dict]:
    """Все сообщения всех каналов владельца по времени мессенджера (у
    недоставленных — по времени попытки)."""
    rows = (
        db.query(ConversationMessage, ContactChannel.channel, User.full_name)
        .join(ContactChannel, ConversationMessage.contact_channel_id == ContactChannel.id)
        .outerjoin(User, ConversationMessage.author_id == User.id)
        .filter(_owner_filter(obj))
        .order_by(func.coalesce(ConversationMessage.sent_at, ConversationMessage.created_at), ConversationMessage.id)
        .all()
    )
    return [
        {
            "id": m.id,
            "channel": kind,
            "direction": m.direction,
            "text": m.text,
            "attachments": m.attachments or [],
            "author_id": m.author_id,
            "author_name": author_name,
            "delivery": m.delivery,
            "error": m.error,
            "sent_at": m.sent_at,
            "created_at": m.created_at,
        }
        for m, kind, author_name in rows
    ]


# -- отправка ---------------------------------------------------------------

def _unavailable(rows: dict[ChannelKind, ContactChannel], kind: ChannelKind) -> ChannelUnavailable:
    """Текст — по запросу заказчика: «Пользователь не найден в Макс.
    Доступные каналы: Telegram, WhatsApp». Доступные — те, что у человека уже
    подключены; если таких нет — настроенные на сервере, куда его можно
    пригласить. Каналы, которых на сервере нет, не предлагаются."""
    label = channels.CHANNEL_LABELS[kind]
    connected = [k for k, r in rows.items() if k != kind and r.status == ChannelStatus.CONNECTED]
    available = connected or [k for k in ChannelKind if k != kind and channels.is_configured(k)]
    row = rows.get(kind)
    head = (
        f"Пользователь отключил бота в {label}."
        if row is not None and row.status == ChannelStatus.STOPPED
        else f"Пользователь не найден в {label}."
    )
    tail = (
        " Доступные каналы: " + ", ".join(channels.CHANNEL_LABELS[k] for k in available)
        if available
        else " Других каналов нет — отправьте человеку ссылку-приглашение."
    )
    return ChannelUnavailable(head + tail, available)


def send(db: Session, obj: Client | Partner, kind: ChannelKind, text: str, author: User) -> ConversationMessage:
    rows = owner_channels(db, obj)
    row = rows.get(kind)
    if row is None or row.status != ChannelStatus.CONNECTED or not row.external_chat_id:
        raise _unavailable(rows, kind)
    adapter = channels.adapter_for(kind)
    if adapter is None or not adapter.configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Канал «{channels.CHANNEL_LABELS[kind]}» не настроен на сервере",
        )

    message = ConversationMessage(
        contact_channel_id=row.id, direction=MessageDirection.OUT, text=text, author_id=author.id
    )
    db.add(message)
    try:
        sent = adapter.send(row.external_chat_id, text)
    except channels.ChannelSendError as exc:
        # Попытку оставляем в ленте с пометкой «не доставлено», а не выдаём за отправленное.
        message.delivery = MessageDelivery.FAILED
        message.error = exc.message
        if exc.stopped:
            row.status = ChannelStatus.STOPPED
        db.commit()
        raise ChannelSendFailed(exc.message) from exc
    message.delivery = MessageDelivery.SENT
    message.external_message_id = sent.external_message_id
    message.sent_at = sent.sent_at or datetime.now(timezone.utc)
    db.flush()
    return message


# -- события мессенджеров (зовут адаптеры каналов) ----------------------------

def connect_by_token(
    db: Session, kind: ChannelKind, token: str | None, external_chat_id: str, user_name: str | None
) -> ContactChannel | None:
    """Человек открыл ссылку-приглашение и нажал «Начать». Неизвестная или
    чужого канала метка — не наша (например, вход сотрудника по диплинку) —
    молча игнорируется."""
    if not token:
        return None
    row = (
        db.query(ContactChannel)
        .filter(ContactChannel.invite_token == token, ContactChannel.channel == kind)
        .one_or_none()
    )
    if row is None:
        return None
    if row.status != ChannelStatus.CONNECTED or row.external_chat_id != external_chat_id:
        row.connected_at = datetime.now(timezone.utc)
    row.status = ChannelStatus.CONNECTED
    row.external_chat_id = external_chat_id
    row.external_user_name = user_name or row.external_user_name
    db.flush()
    return row


def _rows_for_chat(db: Session, kind: ChannelKind, external_chat_id: str) -> list[ContactChannel]:
    return (
        db.query(ContactChannel)
        .filter(ContactChannel.channel == kind, ContactChannel.external_chat_id == external_chat_id)
        .all()
    )


def record_message(
    db: Session,
    kind: ChannelKind,
    external_chat_id: str,
    *,
    external_message_id: str,
    direction: MessageDirection,
    text: str,
    attachments: list | None,
    sent_at: datetime | None,
) -> int:
    """Сообщение из привязанного чата → в ленту каждой карточки, к которой
    этот чат привязан (один человек может быть и клиентом, и партнёром).
    Повтор того же сообщения не дублирует. Возвращает число новых строк."""
    added = 0
    for row in _rows_for_chat(db, kind, external_chat_id):
        exists = (
            db.query(ConversationMessage.id)
            .filter(
                ConversationMessage.contact_channel_id == row.id,
                ConversationMessage.external_message_id == external_message_id,
            )
            .first()
        )
        if exists:
            continue
        db.add(
            ConversationMessage(
                contact_channel_id=row.id,
                direction=direction,
                text=text or "",
                attachments=attachments or [],
                external_message_id=external_message_id,
                delivery=MessageDelivery.SENT,
                sent_at=sent_at,
            )
        )
        added += 1
    db.flush()
    return added


def mark_stopped(db: Session, kind: ChannelKind, external_chat_id: str) -> None:
    for row in _rows_for_chat(db, kind, external_chat_id):
        row.status = ChannelStatus.STOPPED
    db.flush()
