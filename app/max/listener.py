"""Фоновый слушатель аккаунта MAX — новые сообщения в реальном времени (0092).

Обычные запросы /api/max открывают сессию MAX на один запрос и закрывают её,
поэтому о новом сообщении сервер сам не узнаёт. Слушатель держит одну
постоянную авторизованную сессию того же аккаунта (``MAX_TOKEN``) и ловит
push-кадры сервера:

- opcode 1 (``cmd 0``) — пинг сервера; отвечаем ``cmd 1`` с тем же seq;
- opcode 128 (``cmd 0``) — новое сообщение, ``payload.chatId`` и
  ``payload.message.id``; подтверждаем доставку и рассылаем фронту
  ``chat_updated`` (app/max/realtime.py).

Так же делает web.max.ru (обработчик входящих кадров в его клиентском JS,
таблица opcode: LS=1, BS=128). Кадр с seq не больше уже обработанного
пропускаем — сервер может повторить его после сбоя.

Сам сервер не пингует и закрывает молчащую сессию примерно через минуту
(проверено вживую 04.10.2026), поэтому слушатель шлёт пинг (opcode 1,
``{"interactive": false}``) каждые ``PING_INTERVAL`` с — сервер отвечает
``cmd 1``. Обрыв, ошибка или ни одного кадра дольше ``IDLE_TIMEOUT`` →
переподключение с растущей паузой. ``deviceId`` один на процесс: переподключения не плодят у
аккаунта новых «устройств». После каждого переподключения фронту уходит
``resync`` — за время обрыва могли прийти сообщения без push.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid

from app.core.config import settings
from app.max import client as max_client
from app.max import realtime

log = logging.getLogger("app.max.listener")

OP_PING = 1
OP_NOTIF_MESSAGE = 128

# Свой пинг — заметно чаще минуты, после которой MAX закрывает молчащую сессию.
PING_INTERVAL = 25
# Ни одного кадра (даже ответа на пинг) дольше этого — сессия мертва, с.
IDLE_TIMEOUT = 90
# Таймаут одного recv: чтобы вовремя отправлять пинг, не ждём кадр дольше, с.
RECV_TIMEOUT = 5
RETRY_MIN_SECONDS = 5
RETRY_MAX_SECONDS = 300

_started = False
_device_id = str(uuid.uuid4())


class FrameHandler:
    """Разбор входящих кадров одной сессии. ``reply(frame)`` — отправка
    ответного кадра в сокет (в тестах — список)."""

    def __init__(self, reply):
        self.reply = reply
        self.last_seq: int | None = None

    def handle(self, frame: dict) -> None:
        if frame.get("cmd") != 0:
            return  # ответы на наши запросы (cmd 1) и ошибки (cmd 3) слушателю не нужны
        seq = frame.get("seq")
        if isinstance(seq, int):
            if self.last_seq is not None and seq <= self.last_seq:
                return
            self.last_seq = seq
        opcode = frame.get("opcode")
        payload = frame.get("payload") or {}
        if opcode == OP_PING:
            self.reply({"ver": 11, "cmd": 1, "seq": seq, "opcode": OP_PING, "payload": None})
        elif opcode == OP_NOTIF_MESSAGE:
            chat_id = payload.get("chatId")
            message_id = (payload.get("message") or {}).get("id")
            self.reply({
                "ver": 11, "cmd": 1, "seq": seq, "opcode": OP_NOTIF_MESSAGE,
                "payload": {"chatId": chat_id, "messageId": message_id},
            })
            if chat_id is not None:
                try:
                    realtime.chat_updated(int(chat_id))
                except (TypeError, ValueError):
                    log.warning("MAX listener: непонятный chatId в кадре 128: %r", chat_id)


def start_max_listener() -> None:
    """Запустить слушатель в фоновом потоке (один раз на процесс). Без
    ``MAX_TOKEN``, с ``MAX_LIVE_UPDATES=false`` или без websocket-client —
    не запускается: онлайн-обновления нет, остальное работает как раньше."""
    global _started
    if _started or not settings.max_live_updates or not settings.max_token or max_client.websocket is None:
        return
    _started = True
    threading.Thread(target=_run, name="max-listener", daemon=True).start()
    log.info("MAX listener: запущен")


def _open_session() -> max_client.MaxSession:
    session = max_client.MaxSession(settings.max_token)
    session.device_id = _device_id
    session.open()
    session.ws.settimeout(RECV_TIMEOUT)
    return session


def _listen(session: max_client.MaxSession) -> None:
    handler = FrameHandler(lambda frame: session.ws.send(json.dumps(frame)))
    last_ping = last_frame = time.monotonic()
    while True:
        now = time.monotonic()
        if now - last_frame > IDLE_TIMEOUT:
            raise TimeoutError(f"MAX молчит дольше {IDLE_TIMEOUT} с")
        if now - last_ping >= PING_INTERVAL:
            session._send(OP_PING, {"interactive": False})
            last_ping = now
        try:
            frame = session._recv()
        except max_client.websocket.WebSocketTimeoutException:
            continue
        last_frame = time.monotonic()
        handler.handle(frame)


def _run() -> None:
    delay = RETRY_MIN_SECONDS
    while True:
        session = None
        try:
            session = _open_session()
            delay = RETRY_MIN_SECONDS
            log.info("MAX listener: сессия открыта")
            realtime.publish({"type": "resync"})
            _listen(session)
        except Exception as exc:  # noqa: BLE001 — любой сбой = переподключение
            log.warning("MAX listener: сессия прервана (%s), повтор через %s с", exc, delay)
        finally:
            if session is not None:
                session.close()
        time.sleep(delay)
        delay = min(delay * 2, RETRY_MAX_SECONDS)
