"""Рассылка событий чатов MAX открытым вкладкам по WebSocket (0091).

Подписчик — одно WS-подключение (см. ``/ws`` в router.py): у него своя
очередь в event loop, где живёт подключение. ``publish`` можно звать откуда
угодно — из async-эндпоинта (webhook), из sync-эндпоинта в threadpool
(отправка сообщения) или из потока polling: событие кладётся в очереди через
``loop.call_soon_threadsafe`` и уходит клиенту уже из его корутины. Поэтому
медленный клиент не держит приём webhook, а сбой одного не мешает остальным.

Хаб — в памяти процесса: uvicorn запущен одним процессом (entrypoint.sh).
"""

from __future__ import annotations

import asyncio
import logging
import threading

log = logging.getLogger("app.max.realtime")

# Сколько событий копим для одного клиента, пока он не забрал предыдущие.
# Переполнение = клиент завис: лишнее выбрасываем, после переподключения он
# всё равно перечитает данные целиком.
_QUEUE_SIZE = 100


class Subscriber:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=_QUEUE_SIZE)

    def _put(self, event: dict) -> None:
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            log.warning("MAX realtime: очередь подписчика переполнена, событие пропущено")


_subscribers: set[Subscriber] = set()
_lock = threading.Lock()


def subscribe() -> Subscriber:
    """Зарегистрировать подписчика в текущем event loop."""
    sub = Subscriber(asyncio.get_running_loop())
    with _lock:
        _subscribers.add(sub)
    return sub


def unsubscribe(sub: Subscriber) -> None:
    with _lock:
        _subscribers.discard(sub)


def publish(event: dict) -> None:
    """Разослать событие всем подписчикам. Никогда не бросает исключений:
    рассылка не должна ломать запись события в БД."""
    with _lock:
        subs = list(_subscribers)
    for sub in subs:
        try:
            sub.loop.call_soon_threadsafe(sub._put, event)
        except RuntimeError:
            # loop подключения уже закрыт — подписчик умер, не успев отписаться
            unsubscribe(sub)


def chat_updated(chat_id: int | None) -> None:
    """В чате что-то изменилось — фронт перечитает список и ленту сам."""
    if chat_id is not None:
        publish({"type": "chat_updated", "chatId": int(chat_id)})
