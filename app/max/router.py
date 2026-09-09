"""Чтение чатов и сообщений мессенджера MAX (oneme).

Обёртка вокруг websocket-клиента (app/max/client.py). Всё read-only:
списки чатов, история одного чата, ссылки на вложения. Доступ — любой
авторизованный пользователь; данные MAX общие для организации.
"""

from fastapi import Depends, FastAPI
from pydantic import BaseModel, Field

from app.core.deps import get_current_user
from app.max import service as max_service
from app.users.models import User


class SendMessageIn(BaseModel):
    chat_id: int = Field(..., description="ID чата MAX. 0 — «Избранное» (чат с самим собой).")
    text: str = Field(..., min_length=1, max_length=4000)
    notify: bool = True

app = FastAPI(
    title="Soborbum — MAX",
    description="Чтение сообщений из мессенджера MAX (oneme) по websocket.",
    version="0.1.0",
)


@app.get("/chats")
def list_chats(limit: int | None = None, _: User = Depends(get_current_user)):
    """Список чатов с последним сообщением в каждом. ``limit`` — сколько
    самых свежих вернуть (по умолчанию все)."""
    return max_service.list_chats(limit)


@app.get("/chats/{chat_id}")
def get_chat(
    chat_id: int,
    limit: int = 50,
    backward: int = 0,
    _: User = Depends(get_current_user),
):
    """Сообщения одного чата. ``limit`` — сколько последних сообщений,
    ``backward`` — сколько дополнительно подгрузить назад."""
    return max_service.get_chat(chat_id, limit=limit, backward=backward)


@app.post("/messages", status_code=201)
def send_message(payload: SendMessageIn, _: User = Depends(get_current_user)):
    """Отправить текстовое сообщение в чат MAX (MSG_SEND, opcode 64).

    ``chat_id=0`` — «Избранное» (заметки для себя)."""
    return max_service.send_message(payload.chat_id, payload.text, notify=payload.notify)


@app.get("/attachment")
def get_attachment(
    chat_id: int,
    message_id: str,
    file_id: int,
    _: User = Depends(get_current_user),
):
    """Одноразовая ссылка на скачивание вложения типа FILE (``fileId``).

    Домен ``fd.oneme.ru``, без CORS — годится только для навигации/скачивания
    (``window.open`` / ``<a download>``), не для ``fetch``. Для фото берите
    ``attach.baseUrl`` напрямую, для видео/аудио — ``GET /media``."""
    return {"url": max_service.get_attachment_url(chat_id, message_id, file_id)}


@app.get("/media")
def get_media(
    chat_id: int,
    message_id: str,
    media_id: str,
    _: User = Depends(get_current_user),
):
    """Воспроизводимая ссылка на вложение VIDEO или AUDIO (голосовое).

    ``media_id`` — ``videoId`` либо ``audioId`` из attach (строка). Ответ:
    ``{ "url": <прямой MP4 или null>, "external": <веб-плеер ok.ru или null> }``.
    Вложение недоступно/удалено → 422 с текстом причины (фронт показывает
    заглушку), таймаут/сбой MAX → 502."""
    return max_service.get_media_url(chat_id, message_id, media_id)
