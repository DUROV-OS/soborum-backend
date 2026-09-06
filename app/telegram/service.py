"""Telegram-мост: приём апдейтов, архив сообщений, привязка аккаунтов и
одноразовые токены диплинк-логина.

Один и тот же :func:`process_update` вызывают оба источника апдейтов —
long-poll (:mod:`app.telegram.poller`) и вебхук
(``POST /api/telegram/webhook/{secret}``), — поэтому вся логика разбора
живёт здесь, а не в них.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.common.files import FilePurpose, save_bytes_file
from app.core.config import settings
from app.telegram.aliases import normalize_alias
from app.telegram.client import TelegramApiError, TelegramClient
from app.telegram.models import (
    TelegramAccountLink,
    TelegramLoginToken,
    TelegramMessage,
    TelegramPollerState,
)
from app.users.models import User, UserRole

logger = logging.getLogger(__name__)

LOGIN_TOKEN_TTL = timedelta(minutes=15)
# Telegram's own Bot API download cap is 20 MB; stay just under it.
MAX_DOWNLOAD_BYTES = 19 * 1024 * 1024

_bot_username_cache: str | None = None


# --------------------------------------------------------------- helpers --

def _system_user(db: Session) -> User:
    """Owner recorded on FileAssets pulled from Telegram. The first admin;
    falls back to any user so ingest still works on a bare dev DB."""
    user = (
        db.query(User)
        .filter(User.role == UserRole.ADMIN, User.is_active.is_(True))
        .order_by(User.id)
        .first()
    )
    return user or db.query(User).order_by(User.id).first()


def _sender_name(from_user: dict) -> str | None:
    parts = [from_user.get("first_name"), from_user.get("last_name")]
    name = " ".join(p for p in parts if p).strip()
    return name or from_user.get("username")


def _sent_at(message: dict) -> datetime:
    ts = message.get("date")
    if ts is None:
        return datetime.now(timezone.utc)
    return datetime.fromtimestamp(int(ts), tz=timezone.utc)


def bot_username(tg: TelegramClient | None = None) -> str | None:
    """Bot's @username, needed to build the deep link. Cached for the process."""
    global _bot_username_cache
    if _bot_username_cache:
        return _bot_username_cache
    if not settings.telegram_bot_token:
        return None
    try:
        client = tg or TelegramClient()
        _bot_username_cache = client.get_me().get("username")
        if tg is None:
            client.close()
    except TelegramApiError as e:
        logger.warning("getMe failed: %s", e)
        return None
    return _bot_username_cache


# ---------------------------------------------------- poller cursor --

def get_offset(db: Session) -> int:
    state = db.get(TelegramPollerState, 1)
    return (state.last_update_id + 1) if state else 0


def set_last_update_id(db: Session, update_id: int) -> None:
    state = db.get(TelegramPollerState, 1)
    if state is None:
        state = TelegramPollerState(id=1, last_update_id=update_id)
        db.add(state)
    elif update_id > state.last_update_id:
        state.last_update_id = update_id


# ------------------------------------------------ update processing --

def process_update(db: Session, update: dict, tg: TelegramClient | None = None) -> None:
    """Dispatch one raw Telegram update. Commits its own work."""
    update_id = update.get("update_id")
    message = update.get("message") or update.get("channel_post")

    try:
        if message:
            chat = message.get("chat", {})
            chat_id = str(chat.get("id"))
            if chat.get("type") == "private":
                _handle_private_message(db, message, tg)
            elif chat_id == str(settings.telegram_chat_id):
                _record_group_message(db, message, update_id, tg)
            # прочие чаты игнорируем, но offset всё равно двигаем
        if update_id is not None:
            set_last_update_id(db, int(update_id))
        db.commit()
    except Exception:
        db.rollback()
        # Не даём одному битому апдейту застрять навсегда: фиксируем курсор.
        if update_id is not None:
            set_last_update_id(db, int(update_id))
            db.commit()
        logger.exception("Не смог обработать апдейт %s", update_id)


def _record_group_message(
    db: Session, message: dict, update_id: int | None, tg: TelegramClient | None
) -> TelegramMessage | None:
    chat_id = str(message["chat"]["id"])
    message_id = int(message["message_id"])

    existing = (
        db.query(TelegramMessage)
        .filter(TelegramMessage.chat_id == chat_id, TelegramMessage.message_id == message_id)
        .first()
    )
    if existing:
        return existing

    from_user = message.get("from") or {}
    kind, text, file_id, file_unique_id, suggested_name, mime = _classify(message)

    file_asset_id: int | None = None
    if file_id and tg is not None:
        file_asset_id = _try_download(db, tg, file_id, suggested_name, mime)

    row = TelegramMessage(
        chat_id=chat_id,
        message_id=message_id,
        update_id=int(update_id) if update_id is not None else None,
        tg_user_id=str(from_user["id"]) if from_user.get("id") is not None else None,
        tg_username=from_user.get("username"),
        sender_name=_sender_name(from_user),
        kind=kind,
        text=text,
        file_unique_id=file_unique_id,
        file_asset_id=file_asset_id,
        raw=message,
        sent_at=_sent_at(message),
    )
    db.add(row)
    db.flush()
    return row


def _classify(message: dict):
    """-> (kind, text, file_id, file_unique_id, suggested_name, mime_type)."""
    if message.get("photo"):
        largest = max(message["photo"], key=lambda p: p.get("file_size", 0))
        return (
            "photo",
            message.get("caption"),
            largest["file_id"],
            largest.get("file_unique_id"),
            f"photo_{message['message_id']}.jpg",
            "image/jpeg",
        )
    if message.get("document"):
        doc = message["document"]
        return (
            "document",
            message.get("caption"),
            doc["file_id"],
            doc.get("file_unique_id"),
            doc.get("file_name") or f"document_{message['message_id']}",
            doc.get("mime_type") or "application/octet-stream",
        )
    if message.get("text"):
        return ("text", message["text"], None, None, None, None)
    # voice / sticker / video / poll / ... — фиксируем факт, тело пусто
    return ("other", message.get("caption"), None, None, None, None)


def _try_download(
    db: Session, tg: TelegramClient, file_id: str, suggested_name: str | None, mime: str | None
) -> int | None:
    try:
        meta = tg.get_file(file_id)
        if meta.get("file_size", 0) > MAX_DOWNLOAD_BYTES:
            logger.info("Пропускаю крупный файл %s (%s байт)", file_id, meta.get("file_size"))
            return None
        data = tg.download_file(meta["file_path"])
    except (TelegramApiError, KeyError) as e:
        logger.warning("Не смог скачать файл %s: %s", file_id, e)
        return None

    asset = save_bytes_file(
        db,
        filename=suggested_name or file_id,
        data=data,
        content_type=mime or "application/octet-stream",
        purpose=FilePurpose.TELEGRAM_INGEST,
        user=_system_user(db),
    )
    return asset.id


# ---------------------------------------- deep-link login (/start) --

def _handle_private_message(db: Session, message: dict, tg: TelegramClient | None) -> None:
    text = (message.get("text") or "").strip()
    from_user = message.get("from") or {}
    if not text.startswith("/start"):
        return

    parts = text.split(maxsplit=1)
    payload = parts[1].strip() if len(parts) > 1 else ""
    reply: str

    if payload.startswith("login_"):
        token = payload[len("login_"):]
        user = consume_login_token(
            db,
            token,
            tg_user_id=str(from_user.get("id")),
            tg_username=from_user.get("username"),
        )
        reply = (
            f"Готово, {user.full_name}. Этот Telegram-аккаунт привязан к Durov-OS."
            if user
            else "Ссылка недействительна или устарела. Откройте вход из бокового меню Durov-OS ещё раз."
        )
    else:
        reply = (
            "Это служебный бот Durov-OS. Чтобы привязать аккаунт, откройте "
            "«Войти через Telegram» в боковом меню Durov-OS."
        )

    if tg is not None and from_user.get("id") is not None:
        try:
            tg.send_message(from_user["id"], reply)
        except TelegramApiError as e:
            logger.warning("Не смог ответить на /start: %s", e)


def create_login_token(db: Session, user: User) -> TelegramLoginToken:
    token = TelegramLoginToken(
        token=secrets.token_urlsafe(24),
        user_id=user.id,
        expires_at=datetime.now(timezone.utc) + LOGIN_TOKEN_TTL,
    )
    db.add(token)
    db.commit()
    db.refresh(token)
    return token


def consume_login_token(
    db: Session, token_str: str, tg_user_id: str, tg_username: str | None
) -> User | None:
    token = (
        db.query(TelegramLoginToken)
        .filter(TelegramLoginToken.token == token_str)
        .first()
    )
    now = datetime.now(timezone.utc)
    if token is None or token.consumed_at is not None:
        return None
    expires_at = token.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < now:
        return None

    token.consumed_at = now
    token.consumed_by_tg_user_id = str(tg_user_id)
    user = db.get(User, token.user_id)
    if user is None:
        db.commit()
        return None

    link_account(db, tg_user_id=str(tg_user_id), user_id=user.id, tg_username=tg_username)
    db.commit()
    return user


def link_account(
    db: Session,
    *,
    tg_user_id: str,
    user_id: int | None,
    tg_username: str | None = None,
    alias: str | None = None,
) -> TelegramAccountLink:
    link = (
        db.query(TelegramAccountLink)
        .filter(TelegramAccountLink.tg_user_id == str(tg_user_id))
        .first()
    )
    if link is None:
        link = TelegramAccountLink(tg_user_id=str(tg_user_id))
        db.add(link)
    if tg_username is not None:
        link.tg_username = tg_username
    if alias is not None:
        link.alias = normalize_alias(alias)
    if user_id is not None:
        link.user_id = user_id
        link.linked_at = datetime.now(timezone.utc)
    db.flush()
    return link


def unlink_user(db: Session, user_id: int) -> bool:
    link = (
        db.query(TelegramAccountLink)
        .filter(TelegramAccountLink.user_id == user_id)
        .first()
    )
    if link is None:
        return False
    db.delete(link)
    db.commit()
    return True


def get_link_for_user(db: Session, user_id: int) -> TelegramAccountLink | None:
    return (
        db.query(TelegramAccountLink)
        .filter(TelegramAccountLink.user_id == user_id)
        .first()
    )


def deep_link_url(token: str, *, username: str | None = None) -> str | None:
    name = username or bot_username()
    if not name:
        return None
    return f"https://t.me/{name}?start=login_{token}"


# ----------------------------------------------- ingest window read --

def messages_in_window(
    db: Session,
    since: datetime,
    until: datetime,
    *,
    chat_id: str | None = None,
    only_uningested: bool = False,
) -> list[TelegramMessage]:
    query = db.query(TelegramMessage).filter(
        TelegramMessage.sent_at >= since, TelegramMessage.sent_at < until
    )
    if chat_id is not None:
        query = query.filter(TelegramMessage.chat_id == str(chat_id))
    if only_uningested:
        query = query.filter(TelegramMessage.ingested_into_kb_at.is_(None))
    return query.order_by(TelegramMessage.sent_at, TelegramMessage.message_id).all()


def mark_ingested(db: Session, messages: list[TelegramMessage]) -> None:
    now = datetime.now(timezone.utc)
    for m in messages:
        m.ingested_into_kb_at = now
    db.commit()
