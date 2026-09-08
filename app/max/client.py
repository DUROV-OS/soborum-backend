"""Тонкий клиент к мессенджеру MAX (oneme) по websocket.

Протокол — простые JSON-кадры поверх wss: opcode 6 (HELLO) -> 19 (AUTH) ->
49 (история чата) -> 88 (ссылка на вложение). Логика перенесена из
исходного scraping-скрипта (Desktop/max_idi_nahuy/api.py); всё read-only,
БД не трогаем.

Токен берётся из настроек (``settings.max_token`` / переменная ``MAX_TOKEN``).
Обновить: ``JSON.parse(localStorage.__oneme_auth).token`` на web.max.ru.
"""

from __future__ import annotations

import contextlib
import json
import time
import uuid

from fastapi import HTTPException, status

try:  # websocket-client, необязателен для остального приложения
    import websocket  # type: ignore
except ImportError:  # pragma: no cover
    websocket = None

from app.core.config import settings

WS_URL = "wss://ws-api.oneme.ru/websocket"
ORIGIN = "https://web.max.ru"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


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

    def send_message(self, chat_id, text: str, notify: bool = True) -> dict:
        """MSG_SEND (opcode 64). Отправляет текстовое сообщение и возвращает
        payload ответа сервера ({chatId, message, unread, mark})."""
        cid = int(time.time() * 1000)
        seq = self._send(64, {
            "chatId": chat_id,
            "message": {
                "text": text,
                "cid": cid,
                "elements": [],
                "attaches": [],
            },
            "notify": notify,
        })
        reply = self._wait(64, seq)
        # cmd=3 -> сервер отверг запрос (см. max-api-docs/protocol/messaging.md)
        if reply.get("cmd") == 3:
            raise RuntimeError(f"MAX отклонил отправку: {reply.get('payload')}")
        return reply.get("payload", {}) or {}

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
