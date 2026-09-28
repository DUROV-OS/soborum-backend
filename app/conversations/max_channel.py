"""Канал MAX для переписки из карточки (0083-d) — через официального бота (0082).

- Ссылка-приглашение: ``https://max.ru/<ник бота>?start=<метка>`` (dev.max.ru,
  «Работа с диплинками»: payload до 128 символов). Ник бота — из ``GET /me``.
- Человек нажал «Начать» по ссылке → событие ``bot_started`` с ``payload`` =
  метка → чат привязывается к карточке.
- Сообщения привязанного чата → в ленту; ``bot_stopped``/``dialog_removed`` →
  канал STOPPED.

События приходят в ``app.max.ingest.handle_update`` (webhook и polling 0082-a),
оттуда зовётся ``on_update``.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.conversations import channels, service
from app.conversations.models import ChannelKind, MessageDirection
from app.core.config import settings
from app.max import bot_api

log = logging.getLogger("app.conversations.max")

_username: str | None = None
_username_lock = threading.Lock()


def bot_username() -> str | None:
    """Ник бота для диплинка (кэш на процесс). None — MAX не ответил, ссылку
    построим в следующий раз."""
    global _username
    if _username is None:
        with _username_lock:
            if _username is None:
                try:
                    _username = bot_api.get_me().get("username") or None
                except (bot_api.BotApiError, bot_api.BotNotConfigured):
                    return None
    return _username


def _ts(ms: int | None) -> datetime | None:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc) if ms else None


class MaxAdapter:
    kind = ChannelKind.MAX

    def configured(self) -> bool:
        return bool(settings.max_bot_token)

    def invite_link(self, token: str) -> str | None:
        username = bot_username()
        return f"https://max.ru/{username}?start={token}" if username else None

    def send(self, external_chat_id: str, text: str) -> channels.SentMessage:
        try:
            message = bot_api.send_message(int(external_chat_id), text)
        except bot_api.BotNotConfigured as exc:
            raise channels.ChannelSendError(str(exc)) from exc
        except bot_api.BotApiError as exc:
            # 403 chat.denied — человек остановил бота или удалил диалог.
            raise channels.ChannelSendError(exc.message, stopped=exc.status == 403) from exc
        body = message.get("body") or {}
        return channels.SentMessage(
            external_message_id=str(body["mid"]) if body.get("mid") else None,
            sent_at=_ts(message.get("timestamp")),
        )


channels.register(MaxAdapter())


def on_update(db: Session, update: dict) -> None:
    from app.max.ingest import _is_outgoing, user_name

    kind = update.get("update_type")
    if kind == "bot_started":
        chat_id = update.get("chat_id")
        if chat_id is None:
            return
        row = service.connect_by_token(
            db, ChannelKind.MAX, update.get("payload"), str(chat_id), user_name(update.get("user"))
        )
        if row is not None:
            log.info("MAX: чат привязан к карточке по приглашению (канал %s)", row.id)
    elif kind == "message_created":
        message = update.get("message") or {}
        body = message.get("body") or {}
        chat_id = (message.get("recipient") or {}).get("chat_id")
        if chat_id is None or not body.get("mid"):
            return
        service.record_message(
            db,
            ChannelKind.MAX,
            str(chat_id),
            external_message_id=str(body["mid"]),
            direction=MessageDirection.OUT if _is_outgoing(message.get("sender")) else MessageDirection.IN,
            text=body.get("text") or "",
            attachments=body.get("attachments"),
            sent_at=_ts(message.get("timestamp")),
        )
    elif kind in ("bot_stopped", "dialog_removed"):
        chat_id = update.get("chat_id")
        if chat_id is not None:
            service.mark_stopped(db, ChannelKind.MAX, str(chat_id))
