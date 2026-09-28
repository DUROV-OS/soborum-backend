"""Канал Telegram для переписки из карточки (0083-e) — через бота @SoborbumBot.

Та же схема, что у MAX (0083-d): ссылка ``https://t.me/<бот>?start=<метка>`` →
человек жмёт «Запустить» → бот получает ``/start <метка>`` → личный чат
привязывается к карточке. Дальше сообщения этого чата идут в ленту.

Метки, которые не являются приглашением карточки (например ``login_…`` входа
сотрудника из ветки ``telegram``), не трогаются. Сообщения групп и
непривязанных чатов не сохраняются.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.conversations import channels, service
from app.conversations.models import ChannelKind, MessageDirection
from app.conversations import telegram_api
from app.core.config import settings

log = logging.getLogger("app.conversations.telegram")

_username: str | None = None
_username_lock = threading.Lock()
_polling_started = False

# Что из вложений Telegram помечаем в ленте: сами файлы не скачиваются, в
# ленте видно, что человек прислал фото/документ, — открыть можно в Telegram.
ATTACHMENT_KINDS = ("photo", "document", "video", "voice", "audio", "sticker")


def bot_username() -> str | None:
    global _username
    if _username is None:
        with _username_lock:
            if _username is None:
                try:
                    _username = telegram_api.get_me().get("username") or None
                except (telegram_api.TelegramApiError, telegram_api.TelegramNotConfigured):
                    return None
    return _username


def _name(user: dict | None) -> str | None:
    if not user:
        return None
    full = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x)
    return full or user.get("username")


class TelegramAdapter:
    kind = ChannelKind.TELEGRAM

    def configured(self) -> bool:
        return bool(settings.telegram_bot_token)

    def invite_link(self, token: str) -> str | None:
        username = bot_username()
        return f"https://t.me/{username}?start={token}" if username else None

    def send(self, external_chat_id: str, text: str) -> channels.SentMessage:
        try:
            message = telegram_api.send_message(external_chat_id, text)
        except telegram_api.TelegramNotConfigured as exc:
            raise channels.ChannelSendError(str(exc)) from exc
        except telegram_api.TelegramApiError as exc:
            # 403 «bot was blocked by the user» — человек заблокировал бота.
            raise channels.ChannelSendError(exc.message, stopped=exc.status == 403) from exc
        return channels.SentMessage(
            external_message_id=str(message["message_id"]) if message.get("message_id") else None,
            sent_at=datetime.fromtimestamp(message["date"], tz=timezone.utc) if message.get("date") else None,
        )


channels.register(TelegramAdapter())


def handle_update(db: Session, update: dict) -> None:
    """Одно событие Telegram. Идемпотентно: повтор того же message_id не дублирует."""
    message = update.get("message")
    if message:
        chat = message.get("chat") or {}
        if chat.get("type") != "private" or chat.get("id") is None:
            return
        chat_id = str(chat["id"])
        text = message.get("text") or message.get("caption") or ""
        if text.startswith("/start"):
            parts = text.split(maxsplit=1)
            row = service.connect_by_token(
                db, ChannelKind.TELEGRAM, parts[1].strip() if len(parts) > 1 else None, chat_id,
                _name(message.get("from")),
            )
            if row is not None:
                log.info("Telegram: чат привязан к карточке по приглашению (канал %s)", row.id)
            return
        service.record_message(
            db,
            ChannelKind.TELEGRAM,
            chat_id,
            external_message_id=str(message.get("message_id")),
            direction=MessageDirection.IN,
            text=text,
            attachments=[{"type": k} for k in ATTACHMENT_KINDS if message.get(k)],
            sent_at=datetime.fromtimestamp(message["date"], tz=timezone.utc) if message.get("date") else None,
        )
        return
    member = update.get("my_chat_member")
    if member:
        chat = member.get("chat") or {}
        status = (member.get("new_chat_member") or {}).get("status")
        if chat.get("type") == "private" and status == "kicked" and chat.get("id") is not None:
            service.mark_stopped(db, ChannelKind.TELEGRAM, str(chat["id"]))


def handle_updates(db: Session, updates: list[dict]) -> None:
    for update in updates:
        try:
            handle_update(db, update)
            db.commit()
        except Exception:  # noqa: BLE001 — одно кривое событие не роняет пачку
            db.rollback()
            log.exception("Событие Telegram %s не записалось", update.get("update_id"))


def start_telegram_polling_loop() -> None:
    """Long polling getUpdates — только при TELEGRAM_BOT_POLLING=true (локально)."""
    global _polling_started
    if _polling_started or not settings.telegram_bot_polling:
        return
    if not settings.telegram_bot_token:
        log.warning("TELEGRAM_BOT_POLLING включён, но TELEGRAM_BOT_TOKEN не задан — polling не запущен")
        return
    _polling_started = True
    threading.Thread(target=_run_polling, name="telegram-bot-polling", daemon=True).start()
    log.info("Telegram-бот: long polling событий запущен")


def _run_polling() -> None:
    from app.db.session import SessionLocal

    offset: int | None = None
    while True:
        try:
            updates = telegram_api.get_updates(offset=offset, timeout=30)
            if updates:
                db = SessionLocal()
                try:
                    handle_updates(db, updates)
                finally:
                    db.close()
                offset = max(u["update_id"] for u in updates) + 1
        except Exception:  # noqa: BLE001
            log.exception("Telegram-бот: сбой long polling, повтор через 10 с")
            time.sleep(10)
