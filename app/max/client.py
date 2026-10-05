"""Тонкий клиент к мессенджеру MAX (oneme) по websocket.

Протокол — простые JSON-кадры поверх wss: opcode 6 (HELLO) -> 19 (AUTH) ->
49 (история чата) -> 88 (ссылка на вложение); 46/41 — поиск и
добавление контакта по номеру (0093). Логика перенесена из
исходного scraping-скрипта (Desktop/max_idi_nahuy/api.py); в основном
read-only, БД не трогаем — кроме upload_file (0015), которая реально
загружает файл в MAX перед отправкой вложения.

Токен берётся из настроек (``settings.max_token`` / переменная ``MAX_TOKEN``).
Обновить: ``JSON.parse(localStorage.__oneme_auth).token`` на web.max.ru.
"""

from __future__ import annotations

import contextlib
import json
import time
import urllib.parse
import uuid

import httpx
from fastapi import HTTPException, status

try:  # websocket-client, необязателен для остального приложения
    import websocket  # type: ignore
except ImportError:  # pragma: no cover
    websocket = None

from app.core.config import settings

# Больше — отклоняем ещё до попытки загрузки (см. UploadError в service.py).
MAX_UPLOAD_SIZE = 20 * 1024 * 1024
# NOTIF_ATTACH: сервер обработал загруженный файл, его можно прикладывать.
NOTIF_ATTACH = 136
# Сколько ждать NOTIF_ATTACH после загрузки, секунд.
ATTACH_READY_TIMEOUT = 30


class UploadError(RuntimeError):
    """MAX отклонил загрузку файла, либо файл не прошёл локальную проверку
    (см. MAX_UPLOAD_SIZE) — сообщение не уходит наполовину, роутер превращает
    это в понятную ошибку до отправки MSG_SEND."""

WS_URL = "wss://ws-api.oneme.ru/websocket"
ORIGIN = "https://web.max.ru"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class ContactError(RuntimeError):
    """MAX отклонил поиск или добавление контакта. ``code`` — код ошибки MAX
    (``not.found`` — номер не зарегистрирован в MAX)."""

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


class MediaError(RuntimeError):
    """MAX отклонил запрос воспроизводимой ссылки на медиа (нет доступа,
    вложение удалено, неверный id). Фронт по такой ошибке показывает
    заглушку вместо плеера."""


class MaxSession:
    """Одно websocket-соединение; HELLO + AUTH выполняются в open()."""

    def __init__(self, token: str):
        self.token = token
        self.device_id = str(uuid.uuid4())
        self.ws = None
        self.seq = 0
        self.login: dict = {}  # payload кадра AUTH (opcode 19)

    # -- низкий уровень ------------------------------------------------

    def _send(self, opcode: int, payload: dict) -> int:
        self.seq += 1
        self.ws.send(json.dumps({
            "ver": 11, "cmd": 0, "seq": self.seq,
            "opcode": opcode, "payload": payload,
        }))
        return self.seq

    def _recv(self) -> dict:
        raw = self.ws.recv()
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", "replace")
        return json.loads(raw)

    def _wait(self, opcode: int, seq: int | None = None, tries: int = 80) -> dict:
        """Пропускаем технические / асинхронные кадры до нужного нам."""
        for _ in range(tries):
            msg = self._recv()
            if msg.get("opcode") == opcode and (seq is None or msg.get("seq") == seq):
                return msg
        raise TimeoutError(f"нет ответа с opcode {opcode}")

    # -- жизненный цикл ---------------------------------------------------

    def open(self) -> "MaxSession":
        self.ws = websocket.WebSocket()
        self.ws.settimeout(15)
        self.ws.connect(WS_URL, origin=ORIGIN, header=[f"User-Agent: {UA}"])

        s = self._send(6, {
            "userAgent": {
                "deviceType": "WEB", "locale": "ru", "deviceLocale": "ru",
                "osVersion": "macOS", "deviceName": "Chrome",
                "headerUserAgent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "appVersion": "25.12.14", "screen": "982x1512 2.0x",
                "timezone": "Europe/Moscow",
            },
            "deviceId": self.device_id,
        })
        self._wait(6, s)

        # chatsSync=0 -> сервер возвращает полный текущий список чатов
        s = self._send(19, {
            "interactive": False,
            "token": self.token,
            "chatsCount": 40,
            "chatsSync": 0,
            "contactsSync": 0,
            "presenceSync": -1,
            "draftsSync": 0,
        })
        self.login = self._wait(19, s).get("payload", {}) or {}
        return self

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.ws.close()

    # -- данные ------------------------------------------------------

    def chats(self) -> list:
        return self.login.get("chats") or []

    def last_messages(self) -> dict:
        """{ '<chatId>': [msg, ...] } — приходит вместе с ответом AUTH."""
        return self.login.get("messages") or {}

    def contacts_by_id(self) -> dict:
        return {str(c.get("id")): c for c in (self.login.get("contacts") or [])}

    def viewer_id(self) -> str:
        p = self.login.get("profile") or {}
        c = p.get("contact") or {}
        return str(c.get("id") or p.get("id") or "")

    def history(self, chat_id, forward: int = 50, backward: int = 0) -> list:
        # без "from" -> сервер отдаёт последние `forward` сообщений
        self._send(49, {
            "chatId": chat_id,
            "forward": forward,
            "backward": backward,
            "getMessages": True,
        })
        for _ in range(20):
            p = self._wait(49).get("payload", {})
            msgs = p.get("messages")
            if msgs is not None:
                return msgs
        return []

    def send_message(self, chat_id, text: str, notify: bool = True, attaches: list[dict] | None = None) -> dict:
        """MSG_SEND (opcode 64). Отправляет текстовое сообщение (опционально
        с вложениями, полученными через upload_file) и возвращает payload
        ответа сервера ({chatId, message, unread, mark})."""
        cid = int(time.time() * 1000)
        seq = self._send(64, {
            "chatId": chat_id,
            "message": {
                "text": text,
                "cid": cid,
                "elements": [],
                "attaches": attaches or [],
            },
            "notify": notify,
        })
        reply = self._wait(64, seq)
        # cmd=3 -> сервер отверг запрос (см. max-api-docs/protocol/messaging.md)
        if reply.get("cmd") == 3:
            raise RuntimeError(f"MAX отклонил отправку: {reply.get('payload')}")
        return reply.get("payload", {}) or {}

    def upload_file(self, data: bytes, filename: str, content_type: str) -> dict:
        """Загружает файл как вложение FILE: запрашивает у MAX одноразовый
        URL для загрузки (opcode `settings.max_file_upload_opcode`, 87),
        заливает байты, возвращает
        `{"fileId": ..., "token": ...}`; в `attaches` `send_message` идёт
        `{"_type": "FILE", "fileId": ...}` (token MAX не нужен).

        После загрузки ждём NOTIF_ATTACH (136) с этим fileId — так делает
        web.max.ru (`wait_back_processing`): вложение, которое сервер ещё не
        обработал, в сообщение прикладывать нельзя.

        HTTP-часть (заголовки, raw-байты без multipart) разобрана по
        клиентскому JS web.max.ru (`Desktop/max_idi_nahuy/archive`):
        `Content-Type`, `Content-Disposition: attachment`, `Content-Range:
        0-<size-1>/<size>` — так же в оригинале грузятся FILE/VIDEO (в
        отличие от PHOTO, который уходит как multipart/form-data).
        """
        if len(data) > MAX_UPLOAD_SIZE:
            raise UploadError(f"Файл больше {MAX_UPLOAD_SIZE // (1024 * 1024)} МБ — MAX его не примет")

        seq = self._send(settings.max_file_upload_opcode, {"count": 1})
        try:
            reply = self._wait(settings.max_file_upload_opcode, seq)
        except TimeoutError as exc:
            raise UploadError("MAX не ответил на запрос URL для загрузки") from exc
        if reply.get("cmd") == 3:
            raise UploadError(f"MAX отклонил запрос URL для загрузки: {reply.get('payload')}")
        info = ((reply.get("payload") or {}).get("info") or [None])[0]
        if not info or not info.get("url") or info.get("fileId") is None:
            raise UploadError("MAX не вернул URL/fileId для загрузки")

        headers = {
            "Content-Type": content_type or "application/octet-stream",
            "Content-Disposition": f"attachment; filename={urllib.parse.quote(filename)}",
            "Content-Range": f"0-{len(data) - 1}/{len(data)}",
        }
        try:
            resp = httpx.post(info["url"], content=data, headers=headers, timeout=60)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise UploadError(f"MAX отклонил загрузку файла: {exc}") from exc

        self._wait_attach_ready(info["fileId"])
        return {"fileId": info["fileId"], "token": info.get("token")}

    def _wait_attach_ready(self, file_id) -> None:
        deadline = time.monotonic() + ATTACH_READY_TIMEOUT
        while time.monotonic() < deadline:
            try:
                msg = self._recv()
            except Exception as exc:  # noqa: BLE001 — таймаут/обрыв сокета
                raise UploadError("MAX не подтвердил обработку файла") from exc
            if msg.get("opcode") == NOTIF_ATTACH and (msg.get("payload") or {}).get("fileId") == file_id:
                return
        raise UploadError("MAX не подтвердил обработку файла")

    def _request(self, opcode: int, payload: dict) -> dict:
        """Запрос с ответом на тот же seq. ``cmd=3`` — сервер отверг запрос:
        бросаем ContactError с его человеческим текстом и кодом ошибки."""
        seq = self._send(opcode, payload)
        reply = self._wait(opcode, seq)
        if reply.get("cmd") == 3:
            p = reply.get("payload") or {}
            raise ContactError(
                p.get("localizedMessage") or p.get("message") or "MAX отклонил запрос",
                code=p.get("error"),
            )
        return reply.get("payload") or {}

    def contact_by_phone(self, phone: str) -> dict:
        """Пользователь MAX по номеру (opcode 46, CONTACT_INFO_BY_PHONE).
        ``phone`` — только цифры с кодом страны (``79001234567``). Нет такого
        номера в MAX → ContactError с ``code="not.found"``."""
        return self._request(46, {"phone": phone}).get("contact") or {}

    def add_contact(self, phone: str, first_name: str, last_name: str | None = None) -> dict:
        """Добавить в контакты аккаунта по номеру (opcode 41, как «Добавить
        контакт» в web.max.ru): ``{contact, new}``. Имя — то, под которым
        контакт будет виден в MAX и в списке чатов."""
        payload = {"phone": phone, "firstName": first_name}
        if last_name:
            payload["lastName"] = last_name
        return self._request(41, payload)

    def attach_url(self, file_id, chat_id, message_id) -> str:
        self._send(88, {"fileId": file_id, "chatId": chat_id, "messageId": message_id})
        for _ in range(80):
            p = self._wait(88).get("payload", {})
            if p.get("url"):
                return p["url"]
        raise TimeoutError("сервер не вернул ссылку на вложение")

    def media_url(self, media_id, chat_id, message_id) -> dict:
        """Воспроизводимые ссылки на VIDEO и AUDIO (голосовые) вложения.

        Opcode 83 (GET_VIDEO_URL) понимает только поле ``videoId``; для
        голосовых сообщений (в ленте они приходят как ``_type=UNSUPPORTED``
        с ``audioId``) тот же id передаётся в ``videoId``. Токен вложения
        не требуется. Ответ: ``{ "MP4_360"|"MP4_480"|"MP4_720": <url>,
        "EXTERNAL": <url>, "cache": <bool> }`` — набор ключей плавающий.
        """
        self._send(83, {
            "videoId": int(media_id),
            "chatId": chat_id,
            "messageId": message_id,
        })
        for _ in range(80):
            frame = self._wait(83)
            # cmd=3 -> сервер отклонил запрос (нет доступа, id не найден, ...)
            if frame.get("cmd") == 3:
                p = frame.get("payload") or {}
                raise MediaError(
                    p.get("message") or p.get("localizedMessage")
                    or "MAX отклонил запрос медиа"
                )
            p = frame.get("payload") or {}
            if p.get("EXTERNAL") or any(k.startswith("MP4_") for k in p):
                return p
        raise TimeoutError("сервер не вернул ссылку на медиа")


@contextlib.contextmanager
def session():
    """Открытая и авторизованная сессия MAX; сама закрывается по выходу."""
    if websocket is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Пакет websocket-client не установлен",
        )
    if not settings.max_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="MAX_TOKEN не задан в конфигурации",
        )
    try:
        s = MaxSession(settings.max_token).open()
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Не удалось подключиться к MAX: {exc}",
        ) from exc
    try:
        yield s
    finally:
        s.close()
