"""Чаты, лента и отправка сообщений MAX через бота (0082).

Читаем из своей БД (max_bot_chats / max_bot_messages — их наполняют события,
см. app/max/ingest.py), пишем через Bot API (app/max/bot_api.py). Ответы
приводятся к прежней форме frontend/src/max/types.ts, чтобы лента и вложения
на фронте работали без переписывания.
"""

from __future__ import annotations

from typing import Any

import time

import httpx
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.max import bot_api
from app.max.ingest import bot_user_id, store_message
from app.max.models import MaxBotChat, MaxBotMessage


def _sid(v: Any) -> str | None:
    """ID-снежинки MAX (message id, fileId, ...) не влезают в JS Number
    (> 2**53), поэтому отдаём их строкой — иначе фронт округлит и не
    сможет вернуть точное значение в /attachment."""
    return None if v is None else str(v)


def _bot_error(exc: Exception) -> HTTPException:
    """Сбой Bot API → понятная HTTP-ошибка: нет токена — 503, отказ или
    молчание MAX — 502 с причиной (``chat.denied`` = бот не админ группы
    или пользователь остановил бота)."""
    if isinstance(exc, bot_api.BotNotConfigured):
        return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
    if isinstance(exc, bot_api.BotApiError) and exc.code == "chat.denied":
        return HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="MAX запретил боту писать в этот чат (chat.denied): бот не администратор "
            "группы или пользователь остановил бота",
        )
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))


# Тип вложения Bot API → тип в форме фронта (frontend/src/max/types.ts).
_ATTACH_TYPES = {"image": "PHOTO", "file": "FILE", "video": "VIDEO", "audio": "AUDIO", "share": "SHARE"}


def _fmt_attach(a: dict, index: int) -> dict:
    """Вложение Bot API → прежняя форма ``MaxAttach``. У Bot API нет
    отдельных id файла/видео/аудио, которые можно вернуть в /attachment и
    /media, поэтому ``fileId``/``videoId``/``audioId`` — индекс вложения в
    сообщении: по нему ссылка берётся из сохранённого payload."""
    kind = _ATTACH_TYPES.get(a.get("type"), "UNSUPPORTED")
    payload = a.get("payload") or {}
    idx = str(index)
    d: dict[str, Any] = {"type": kind}
    if kind == "PHOTO":
        d["baseUrl"] = payload.get("url")
        d["photoId"] = _sid(payload.get("photo_id"))
    elif kind == "FILE":
        d["name"] = a.get("filename")
        d["size"] = a.get("size")
        d["fileId"] = idx
    elif kind == "VIDEO":
        d["videoId"] = idx
        d["thumbnail"] = (a.get("thumbnail") or {}).get("url")
        # Bot API отдаёт длительность в секундах, фронт ждёт миллисекунды
        d["duration"] = a["duration"] * 1000 if a.get("duration") else None
    elif kind == "AUDIO":
        d["audioId"] = idx
    elif kind == "SHARE":
        d["url"] = payload.get("url")
        d["title"] = a.get("title")
    return {k: v for k, v in d.items() if v is not None}


def _fmt_msg(row: MaxBotMessage | None) -> dict | None:
    if row is None:
        return None
    return {
        "id": row.mid,
        "time": row.timestamp,
        "senderId": _sid(row.sender_id),
        "senderName": row.sender_name,
        # исходящее = отправлено ботом (определяется при сохранении, см. ingest)
        "isOutgoing": bool(row.is_outgoing),
        # служебных CONTROL-сообщений у Bot API нет: вступление/выход приходят
        # отдельными событиями и в ленту не попадают
        "isSystem": False,
        "systemText": None,
        "text": row.text or "",
        "elements": [],
        "attaches": [_fmt_attach(a, i) for i, a in enumerate(row.attachments or [])],
    }


# Тип чата Bot API → тип в форме фронта.
_CHAT_TYPES = {"dialog": "DIALOG", "chat": "CHAT", "channel": "CHANNEL"}


def _chat_title(chat: MaxBotChat) -> str:
    return chat.title or str(chat.chat_id)


def _last_message(db: Session, chat_id: int) -> MaxBotMessage | None:
    return (
        db.query(MaxBotMessage)
        .filter(MaxBotMessage.chat_id == chat_id, MaxBotMessage.deleted.is_(False))
        .order_by(MaxBotMessage.timestamp.desc())
        .first()
    )


def list_chats(db: Session, limit: int | None = None) -> dict[str, Any]:
    """Чаты, где бот сейчас состоит (из max_bot_chats — списка чатов у Bot
    API нет), самые свежие сверху."""
    q = (
        db.query(MaxBotChat)
        .filter(MaxBotChat.status == "active")
        .order_by(MaxBotChat.last_event_time.desc().nullslast())
    )
    if limit:
        q = q.limit(limit)
    items = []
    for chat in q:
        last = _last_message(db, chat.chat_id)
        items.append({
            "id": chat.chat_id,
            "type": _CHAT_TYPES.get(chat.type, chat.type.upper()),
            "title": _chat_title(chat),
            "unread": chat.unread or 0,
            "lastEventTime": chat.last_event_time or (last.timestamp if last else None),
            "lastMessage": _fmt_msg(last),
        })
    return {"count": len(items), "chats": items}


def _known_chat(db: Session, chat_id: int) -> MaxBotChat:
    chat = db.get(MaxBotChat, chat_id)
    if chat is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Бот не состоит в этом чате")
    return chat


def get_chat(db: Session, chat_id: int, limit: int = 50, backward: int = 0) -> dict[str, Any]:
    """Последние ``limit`` сообщений чата плюс ``backward`` более старых, по
    возрастанию времени. Открытие чата сбрасывает непрочитанное."""
    chat = _known_chat(db, chat_id)
    rows = (
        db.query(MaxBotMessage)
        .filter(MaxBotMessage.chat_id == chat_id, MaxBotMessage.deleted.is_(False))
        .order_by(MaxBotMessage.timestamp.desc())
        .limit(max(limit, 0) + max(backward, 0))
        .all()
    )
    rows.reverse()
    if chat.unread:
        chat.unread = 0
        db.commit()
    bot_id = bot_user_id()
    return {
        "chatId": chat_id,
        "title": _chat_title(chat),
        "viewerId": _sid(bot_id) or "",
        "isGroup": chat.type != "dialog",
        "count": len(rows),
        "messages": [_fmt_msg(r) for r in rows],
    }


# Больше — отклоняем ещё до загрузки в MAX.
MAX_UPLOAD_SIZE = 20 * 1024 * 1024

# Загруженный файл MAX обрабатывает не сразу: отправка с его токеном до
# готовности отвечает attachment.not.ready — повторяем с растущей паузой.
ATTACHMENT_RETRIES = 5
ATTACHMENT_RETRY_DELAY = 1.0


def _upload_file(data: bytes, filename: str, content_type: str) -> dict:
    """``POST /uploads?type=file`` → заливка на одноразовый URL → вложение
    ``{type: file, payload: {token}}`` для ``POST /messages``."""
    if len(data) > MAX_UPLOAD_SIZE:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Файл больше {MAX_UPLOAD_SIZE // (1024 * 1024)} МБ — MAX его не примет",
        )
    try:
        target = bot_api.get_upload_url("file")
        if not target.get("url"):
            raise bot_api.BotApiError(None, None, "MAX не вернул URL для загрузки файла")
        uploaded = bot_api.upload(target["url"], data, filename, content_type)
    except (bot_api.BotApiError, bot_api.BotNotConfigured) as exc:
        raise _bot_error(exc) from exc
    token = uploaded.get("token") or target.get("token")
    if not token:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="MAX не вернул токен загруженного файла")
    return {"type": "file", "payload": {"token": token}}


def _send(chat_id: int, text: str, attachments: list[dict] | None, notify: bool) -> dict:
    for attempt in range(ATTACHMENT_RETRIES + 1):
        try:
            return bot_api.send_message(chat_id, text, attachments, notify=notify)
        except bot_api.BotApiError as exc:
            if exc.code != "attachment.not.ready" or attempt == ATTACHMENT_RETRIES:
                raise _bot_error(exc) from exc
            time.sleep(ATTACHMENT_RETRY_DELAY * (attempt + 1))
        except bot_api.BotNotConfigured as exc:
            raise _bot_error(exc) from exc
    raise AssertionError("unreachable")


def send_message(
    chat_id,
    text: str,
    notify: bool = True,
    file: tuple[bytes, str, str] | None = None,
    db: Session | None = None,
) -> dict[str, Any]:
    """Текст и/или файл (``file`` — ``(данные, имя, content_type)``) через
    бота. Пустой текст допустим только вместе с файлом. Отправленное
    сохраняется в ленту как исходящее; при отказе MAX — ничего не пишем.
    ``db`` необязателен (так зовёт warehouse.send_lead_time_question) —
    тогда открываем свою сессию."""
    text = (text or "").strip()
    if not text and not file:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Пустой текст или вложение")
    attachments = [_upload_file(*file)] if file is not None else None
    sent = _send(int(chat_id), text, attachments, notify)

    own = db is None
    db = db or SessionLocal()
    try:
        row = store_message(db, sent, count_unread=False)
        if row is not None:
            row.is_outgoing = True
        db.commit()
        return {"chatId": int(chat_id), "message": _fmt_msg(row)}
    finally:
        if own:
            db.close()


def _stored_attach(db: Session, chat_id: int, message_id: str, index: int) -> dict:
    """Вложение сохранённого сообщения по индексу (так фронт адресует
    fileId/videoId/audioId — см. _fmt_attach)."""
    row = db.get(MaxBotMessage, message_id)
    attachments = (row.attachments or []) if row is not None and row.chat_id == chat_id else []
    if not 0 <= index < len(attachments):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Вложение не найдено")
    return attachments[index]


def _index(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Вложение не найдено") from exc


def get_attachment_url(db: Session, chat_id: int, message_id: str, file_id) -> str:
    attach = _stored_attach(db, chat_id, message_id, _index(file_id))
    url = (attach.get("payload") or {}).get("url")
    if not url:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="MAX не прислал ссылку на файл")
    return url


# Расширения, которые можно показать прямо в браузере (0031). Список — сами
# content-type: MAX не отдаёт mime-type вложения, только имя файла, поэтому
# тип определяем строго по этой карте, не через mimetypes.guess_type — так
# эндпоинт не превращается в открытый прокси произвольного content-type по
# присланному расширению.
PREVIEWABLE_EXTENSIONS: dict[str, str] = {
    "pdf": "application/pdf",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "txt": "text/plain; charset=utf-8",
}

# Крупный файл не тащим в браузер — сразу отдаём как для скачивания
# (см. GET /attachment, одноразовая ссылка без прокси).
PREVIEW_MAX_SIZE = 15 * 1024 * 1024


def get_attachment_preview(db: Session, chat_id: int, message_id: str, file_id, filename: str) -> tuple[bytes, str]:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    content_type = PREVIEWABLE_EXTENSIONS.get(ext)
    if content_type is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Предпросмотр не поддерживается для .{ext or '?'} — скачайте файл",
        )

    url = get_attachment_url(db, chat_id, message_id, file_id)
    try:
        with httpx.stream("GET", url, timeout=30, follow_redirects=True) as resp:
            resp.raise_for_status()
            content_length = resp.headers.get("content-length")
            if content_length and int(content_length) > PREVIEW_MAX_SIZE:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail="Файл больше 15 МБ — скачайте вместо предпросмотра",
                )
            chunks: list[bytes] = []
            total = 0
            for chunk in resp.iter_bytes():
                total += len(chunk)
                if total > PREVIEW_MAX_SIZE:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail="Файл больше 15 МБ — скачайте вместо предпросмотра",
                    )
                chunks.append(chunk)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    return b"".join(chunks), content_type


def _best_mp4(urls: dict) -> str | None:
    """Из ``urls`` ответа ``GET /videos/{token}`` — самый качественный MP4."""
    def _res(key: str) -> int:
        tail = key.split("_", 1)[1]
        return int(tail) if tail.isdigit() else 0

    keys = sorted((k for k in urls if k.startswith("mp4_") and urls[k]), key=_res, reverse=True)
    return urls[keys[0]] if keys else None


def get_media_url(db: Session, chat_id: int, message_id: str, media_id) -> dict[str, Any]:
    """Воспроизводимая ссылка на VIDEO или AUDIO вложение.

    ``media_id`` — ``videoId``/``audioId`` из attach (индекс вложения).
    Видео — через ``GET /videos/{token}`` (лучший ``mp4_*``, запасная —
    ссылка из payload), аудио — ссылка из payload. Возвращает
    ``{ "url": <прямой файл | None>, "external": <запасная | None> }``.
    """
    attach = _stored_attach(db, chat_id, message_id, _index(media_id))
    payload = attach.get("payload") or {}
    url, external = None, None
    if attach.get("type") == "video":
        if payload.get("token"):
            try:
                info = bot_api.get_video(payload["token"])
            except bot_api.BotNotConfigured as exc:
                raise _bot_error(exc) from exc
            except bot_api.BotApiError as exc:
                # видео удалено / нет доступа — фронт покажет заглушку
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
            url = _best_mp4(info.get("urls") or {})
        external = payload.get("url")
    elif attach.get("type") == "audio":
        url = payload.get("url")
    if not url and not external:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="MAX не вернул воспроизводимую ссылку",
        )
    return {"url": url, "external": external}
