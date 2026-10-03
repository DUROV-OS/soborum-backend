"""Разбор событий (Update) MAX-бота в таблицы max_bot_chats / max_bot_messages.

Одна точка входа для webhook и long polling — ``handle_update``. Идемпотентна:
MAX повторяет доставку webhook при сбое, а polling может отдать событие ещё
раз, поэтому повторный ``message_created`` с тем же mid не плодит строк и не
накручивает непрочитанное.
"""

from __future__ import annotations

import logging
import threading
import time

from sqlalchemy.orm import Session

from app.max import bot_api
from app.max.models import MaxBotChat, MaxBotMessage

log = logging.getLogger("app.max.ingest")

_bot_id: int | None = None
_bot_id_lock = threading.Lock()
# После неудачного GET /me не спрашиваем MAX снова раньше этого срока: иначе
# при недоступном MAX каждое событие webhook и каждое открытие ленты ждали бы
# таймаут запроса.
_BOT_ID_RETRY_SECONDS = 60
_bot_id_failed_at: float | None = None


def bot_user_id() -> int | None:
    """user_id самого бота (из GET /me, кэш на процесс) — по нему сообщение
    считается исходящим. None, если MAX недоступен: тогда исходящим считаем
    сообщение с ``sender.is_bot`` (см. _is_outgoing)."""
    global _bot_id, _bot_id_failed_at
    if _bot_id is None:
        with _bot_id_lock:
            if _bot_id is None:
                if _bot_id_failed_at is not None and time.monotonic() - _bot_id_failed_at < _BOT_ID_RETRY_SECONDS:
                    return None
                try:
                    _bot_id = int(bot_api.get_me()["user_id"])
                except (bot_api.BotApiError, bot_api.BotNotConfigured, KeyError, TypeError, ValueError):
                    _bot_id_failed_at = time.monotonic()
                    return None
    return _bot_id


def user_name(user: dict | None) -> str | None:
    if not user:
        return None
    full = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x)
    return full or user.get("name") or user.get("username")


def _is_outgoing(sender: dict | None) -> bool:
    if not sender:
        return False
    bid = bot_user_id()
    if bid is not None:
        return sender.get("user_id") == bid
    return bool(sender.get("is_bot"))


def _chat(db: Session, chat_id: int, chat_type: str | None = None) -> MaxBotChat:
    chat = db.get(MaxBotChat, chat_id)
    if chat is None:
        chat = MaxBotChat(chat_id=chat_id, type=chat_type or "dialog", status="active", unread=0)
        db.add(chat)
    elif chat_type:
        chat.type = chat_type
    return chat


def _fill_group_title(chat: MaxBotChat) -> None:
    """У группы название в событиях не приходит — один раз спрашиваем GET
    /chats/{id}. Не получилось — не беда, покажем id."""
    if chat.title or chat.type == "dialog":
        return
    try:
        info = bot_api.get_chat(chat.chat_id)
    except (bot_api.BotApiError, bot_api.BotNotConfigured):
        return
    chat.title = info.get("title") or chat.title


def store_message(db: Session, message: dict, *, count_unread: bool = True) -> MaxBotMessage | None:
    """Сохранить Message Bot API (входящее из события или отправленное ботом).
    Возвращает строку сообщения; None, если у сообщения нет mid/чата."""
    body = message.get("body") or {}
    recipient = message.get("recipient") or {}
    mid = body.get("mid")
    chat_id = recipient.get("chat_id")
    if not mid or chat_id is None:
        return None
    sender = message.get("sender") or {}
    outgoing = _is_outgoing(sender)
    ts = message.get("timestamp") or 0

    chat = _chat(db, int(chat_id), recipient.get("chat_type"))
    chat.status = "active"
    if chat.type == "dialog" and not outgoing and sender.get("user_id") is not None:
        chat.dialog_user_id = sender["user_id"]
        chat.title = chat.title or user_name(sender)
    _fill_group_title(chat)
    if ts and (chat.last_event_time or 0) < ts:
        chat.last_event_time = ts

    row = db.get(MaxBotMessage, str(mid))
    if row is None:
        row = MaxBotMessage(mid=str(mid), chat_id=int(chat_id), timestamp=ts, is_outgoing=outgoing)
        db.add(row)
        if count_unread and not outgoing:
            chat.unread = (chat.unread or 0) + 1
    row.seq = body.get("seq")
    row.sender_id = sender.get("user_id")
    row.sender_name = user_name(sender)
    row.text = body.get("text") or ""
    row.attachments = body.get("attachments") or []
    return row


def handle_update(db: Session, update: dict) -> None:
    kind = update.get("update_type")
    if kind in ("message_created", "message_edited"):
        if update.get("message"):
            store_message(db, update["message"])
    elif kind == "message_removed":
        row = db.get(MaxBotMessage, str(update.get("message_id")))
        if row is not None:
            row.deleted = True
    elif kind in ("bot_started", "bot_added"):
        chat_id = update.get("chat_id")
        if chat_id is None:
            return
        is_group = kind == "bot_added"
        chat = _chat(db, int(chat_id), ("channel" if update.get("is_channel") else "chat") if is_group else "dialog")
        chat.status = "active"
        user = update.get("user") or {}
        if not is_group:
            chat.dialog_user_id = user.get("user_id")
            chat.title = chat.title or user_name(user)
        _fill_group_title(chat)
        ts = update.get("timestamp")
        if ts and (chat.last_event_time or 0) < ts:
            chat.last_event_time = ts
    elif kind in ("bot_removed", "bot_stopped", "dialog_removed"):
        chat = db.get(MaxBotChat, update.get("chat_id"))
        if chat is not None:
            chat.status = "removed"
    elif kind == "chat_title_changed":
        chat = db.get(MaxBotChat, update.get("chat_id"))
        if chat is not None and update.get("title"):
            chat.title = update["title"]
    else:
        return
    # Переписка из карточек клиентов/партнёров (0083-d): привязка чата по
    # ссылке-приглашению и лента. Импорт здесь — conversations сам зовёт ingest.
    from app.conversations import max_channel

    max_channel.on_update(db, update)
    db.commit()


def handle_updates(db: Session, updates: list[dict]) -> None:
    for u in updates:
        try:
            handle_update(db, u)
        except Exception:  # noqa: BLE001 — одно кривое событие не должно ронять пачку
            db.rollback()
            log.exception("Событие MAX-бота %s не записалось", u.get("update_type"))
