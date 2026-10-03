"""Мессенджер MAX через официального бота (0082).

Чаты и лента — из своей БД (их наполняют события бота, см. /webhook и
app/max/polling.py), отправка и вложения — через Bot API. Доступ — любой
авторизованный пользователь; данные MAX общие для организации.
"""

import hmac

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request, Response, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.clients.models import Client, ClientChatLink
from app.core.config import settings
from app.core.deps import get_current_user
from app.db.session import get_db
from app.max import service as max_service
from app.max.ingest import handle_updates
from app.users.models import User


class SendMessageIn(BaseModel):
    chat_id: int = Field(..., description="ID чата MAX, где состоит бот.")
    text: str = Field(..., min_length=1, max_length=4000)
    notify: bool = True

app = FastAPI(
    title="Soborbum — MAX",
    description="Чаты и сообщения мессенджера MAX через официального бота (Bot API).",
    version="0.1.0",
)


@app.get("/chats")
def list_chats(
    limit: int | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Чаты, где состоит бот, с последним сообщением в каждом (из БД — списка
    чатов у Bot API нет). ``limit`` — сколько
    самых свежих вернуть (по умолчанию все). Каждый чат дополнительно
    аннотирован ``linkedClientId``/``linkedClientName``, если он привязан к
    клиенту (app.clients) — для обратной привязки «из MAX к клиенту»."""
    result = max_service.list_chats(db, limit)
    linked = {
        max_chat_id: (client_id, client_name)
        for max_chat_id, client_id, client_name in db.query(
            ClientChatLink.max_chat_id, Client.id, Client.full_name
        ).join(Client, Client.id == ClientChatLink.client_id)
    }
    for chat in result["chats"]:
        client_id, client_name = linked.get(chat["id"], (None, None))
        chat["linkedClientId"] = client_id
        chat["linkedClientName"] = client_name
    return result


@app.get("/bot")
def get_bot(_: User = Depends(get_current_user)):
    """Профиль бота для подсказок в интерфейсе: ``{name, username, link}``,
    ``link`` открывает бота в MAX. Нет токена → 503, MAX недоступен → 502."""
    return max_service.bot_profile()


@app.get("/chats/{chat_id}")
def get_chat(
    chat_id: int,
    limit: int = 50,
    backward: int = 0,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Сообщения одного чата из БД бота. ``limit`` — сколько последних
    сообщений, ``backward`` — сколько дополнительно подгрузить назад. Чат,
    которого бот не видел, → 404."""
    return max_service.get_chat(db, chat_id, limit=limit, backward=backward)


@app.post("/messages", status_code=201)
def send_message(
    payload: SendMessageIn,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Отправить текстовое сообщение в чат от имени бота
    (``POST /messages?chat_id=`` Bot API). Отправленное попадает в ленту
    как исходящее."""
    return max_service.send_message(payload.chat_id, payload.text, notify=payload.notify, db=db)


@app.post("/messages/attachment", status_code=201)
async def send_message_with_attachment(
    chat_id: int = Form(..., description="ID чата MAX, где состоит бот."),
    text: str = Form("", max_length=4000),
    notify: bool = Form(True),
    file: UploadFile | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Отправить сообщение с файлом (0015): файл грузится в MAX
    (``POST /uploads?type=file``) и уходит вложением. Отдельный эндпоинт
    (не `/messages`) — тот принимает чистый JSON, здесь нужен multipart.
    Текст необязателен, если есть файл; без файла и текста — 422; файл
    больше 20 МБ — 413."""
    upload = None
    if file is not None:
        upload = (await file.read(), file.filename or "file", file.content_type or "application/octet-stream")
    return max_service.send_message(chat_id, text, notify=notify, file=upload, db=db)


@app.get("/attachment")
def get_attachment(
    chat_id: int,
    message_id: str,
    file_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Ссылка на скачивание вложения FILE. ``file_id`` — ``fileId`` из
    attach (индекс вложения в сообщении); ссылка берётся из сохранённого
    сообщения. Годится для навигации/скачивания (``window.open`` /
    ``<a download>``), не для ``fetch`` — для этого `/attachment/preview`.
    Для фото берите ``attach.baseUrl`` напрямую, для видео/аудио —
    ``GET /media``."""
    return {"url": max_service.get_attachment_url(db, chat_id, message_id, file_id)}


@app.get("/attachment/preview")
def get_attachment_preview(
    chat_id: int,
    message_id: str,
    file_id: int,
    filename: str,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Прокси вложения FILE для показа в приложении (0031), не для скачивания.

    Бэк сам скачивает файл по ссылке из `GET /attachment` и отдаёт его с тем
    же CORS, что и весь `/api`, так что подходит для `fetch`/`<img>`/
    `<embed>`. ``filename`` — только для определения content-type по
    расширению; тип ограничен списком в `max_service.PREVIEWABLE_EXTENSIONS`,
    остальное — 422, чтобы фронт откатился на кнопку «Скачать». Больше
    15 МБ — 413, тоже откат на скачивание."""
    data, content_type = max_service.get_attachment_preview(db, chat_id, message_id, file_id, filename)
    return Response(content=data, media_type=content_type)


@app.get("/media")
def get_media(
    chat_id: int,
    message_id: str,
    media_id: str,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Воспроизводимая ссылка на вложение VIDEO или AUDIO.

    ``media_id`` — ``videoId`` либо ``audioId`` из attach. Ответ:
    ``{ "url": <прямой MP4/аудио или null>, "external": <запасная ссылка или null> }``.
    Вложение недоступно/удалено → 422 с текстом причины (фронт показывает
    заглушку), сбой MAX → 502."""
    return max_service.get_media_url(db, chat_id, message_id, media_id)


@app.post("/webhook")
async def bot_webhook(
    request: Request,
    x_max_bot_api_secret: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    """Приём событий MAX-бота (0082): сюда MAX шлёт Update по подписке
    ``POST /subscriptions`` (см. scripts/max_bot_subscribe.py). Без JWT —
    вместо него секрет подписки в заголовке ``X-Max-Bot-Api-Secret``; секрет
    не задан или не совпал → 403 и ничего не пишется. Отвечаем сразу: MAX
    ждёт 200 не дольше 30 с, иначе повторяет доставку."""
    expected = settings.max_webhook_secret
    if not expected or not hmac.compare_digest(x_max_bot_api_secret or "", expected):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Неверный секрет webhook")
    body = await request.json()
    updates = body.get("updates") if isinstance(body, dict) and "updates" in body else [body]
    handle_updates(db, [u for u in updates if isinstance(u, dict)])
    return {"ok": True}
