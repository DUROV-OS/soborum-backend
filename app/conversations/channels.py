"""Реестр каналов переписки (0083-d): как для каждого мессенджера строится
ссылка-приглашение и как отправляется сообщение.

Адаптер канала регистрируется своим модулем (MAX — app/conversations/max_channel.py,
Telegram — 0083-e, WhatsApp — 0083-f). Канал без адаптера или без токена на
сервере — ``configured = False``: в карточке он показывается «не настроен» и
в «Доступные каналы» не попадает.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.conversations.models import ChannelKind

CHANNEL_LABELS = {
    ChannelKind.MAX: "Макс",
    ChannelKind.TELEGRAM: "Telegram",
    ChannelKind.WHATSAPP: "WhatsApp",
}


class ChannelSendError(RuntimeError):
    """Мессенджер не принял сообщение. ``stopped`` — человек остановил или
    заблокировал бота: канал переводится в STOPPED, писать туда больше нельзя."""

    def __init__(self, message: str, *, stopped: bool = False):
        super().__init__(message)
        self.message = message
        self.stopped = stopped


@dataclass
class SentMessage:
    external_message_id: str | None
    sent_at: datetime | None


class ChannelAdapter(Protocol):
    kind: ChannelKind

    def configured(self) -> bool: ...

    def invite_link(self, token: str) -> str | None:
        """Ссылка на бота с меткой; None — ссылку сейчас не построить
        (например, мессенджер не ответил на запрос имени бота)."""

    def send(self, external_chat_id: str, text: str) -> SentMessage: ...


_ADAPTERS: dict[ChannelKind, ChannelAdapter] = {}


def register(adapter: ChannelAdapter) -> None:
    _ADAPTERS[adapter.kind] = adapter


def adapter_for(kind: ChannelKind) -> ChannelAdapter | None:
    return _ADAPTERS.get(kind)


def is_configured(kind: ChannelKind) -> bool:
    adapter = adapter_for(kind)
    return bool(adapter and adapter.configured())
