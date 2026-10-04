"""Онлайн-обновление чатов MAX (0092): WS /api/max/ws и слушатель аккаунта.

Кадры MAX — по обработчику входящих кадров web.max.ru (opcode 1 — пинг,
128 — новое сообщение); значения синтетические, реальный MAX не трогаем.
"""

import contextlib
import threading
import time

import pytest
from starlette.websockets import WebSocketDisconnect

from app.core.config import settings
from app.core.security import create_access_token
from app.max import listener, realtime
from app.max import router as max_router
from app.max import service as max_service


def _wait_no_subscribers():
    for _ in range(50):
        if not realtime._subscribers:
            return
        time.sleep(0.02)
    pytest.fail("подписчик не отписался после закрытия соединения")


@contextlib.contextmanager
def _authed_ws(client, user, *, wait_unsubscribed=True):
    with client.websocket_connect("/api/max/ws") as conn:
        conn.send_json({"type": "auth", "token": create_access_token(str(user.id))})
        assert conn.receive_json() == {"type": "ready"}
        yield conn
    if wait_unsubscribed:
        _wait_no_subscribers()


# --- WebSocket /api/max/ws -------------------------------------------------


def test_push_from_listener_reaches_two_tabs(api, make_user):
    user = make_user()
    client = api(user)
    replies = []
    handler = listener.FrameHandler(replies.append)
    with _authed_ws(client, user) as a, _authed_ws(client, user, wait_unsubscribed=False) as b:
        handler.handle({"ver": 11, "cmd": 0, "seq": 7, "opcode": 128,
                        "payload": {"chatId": 555, "message": {"id": "9001", "text": "привет"}}})
        assert a.receive_json() == {"type": "chat_updated", "chatId": 555}
        assert b.receive_json() == {"type": "chat_updated", "chatId": 555}


class _FakeSession:
    def send_message(self, chat_id, text, notify=True, attaches=None):
        return {"chatId": chat_id, "message": {"id": "1", "time": 1, "text": text, "attaches": []}}

    def viewer_id(self):
        return "1"

    def contacts_by_id(self):
        return {}


def test_sent_message_is_pushed_to_other_tabs(monkeypatch, api, make_user):
    @contextlib.contextmanager
    def _session():
        yield _FakeSession()

    monkeypatch.setattr(max_service, "session", _session)
    user = make_user()
    client = api(user)
    with _authed_ws(client, user) as conn:
        r = client.post("/api/max/messages", json={"chat_id": 555, "text": "ответ"})
        assert r.status_code == 201
        assert conn.receive_json() == {"type": "chat_updated", "chatId": 555}


@pytest.mark.parametrize(
    "first",
    [
        {"type": "auth", "token": "мусор"},
        {"type": "auth"},
        {"type": "hello", "token": "подставится ниже"},
        ["не", "объект"],
    ],
)
def test_bad_auth_is_closed_4401(api, make_user, first):
    user = make_user()
    client = api(user)
    if isinstance(first, dict) and first.get("type") == "hello":
        first = {"type": "hello", "token": create_access_token(str(user.id))}
    with client.websocket_connect("/api/max/ws") as conn:
        conn.send_json(first)
        with pytest.raises(WebSocketDisconnect) as exc:
            conn.receive_json()
    assert exc.value.code == max_router.WS_UNAUTHORIZED
    assert not realtime._subscribers


def test_inactive_user_is_closed_4401(api, make_user, db):
    user = make_user()
    user.is_active = False
    db.commit()
    client = api(user)
    with client.websocket_connect("/api/max/ws") as conn:
        conn.send_json({"type": "auth", "token": create_access_token(str(user.id))})
        with pytest.raises(WebSocketDisconnect) as exc:
            conn.receive_json()
    assert exc.value.code == max_router.WS_UNAUTHORIZED


def test_no_auth_in_time_is_closed_4401(monkeypatch, api, make_user):
    monkeypatch.setattr(max_router, "WS_AUTH_TIMEOUT", 0.1)
    client = api(make_user())
    with client.websocket_connect("/api/max/ws") as conn:
        with pytest.raises(WebSocketDisconnect) as exc:
            conn.receive_json()
    assert exc.value.code == max_router.WS_UNAUTHORIZED


def test_publish_with_dead_loop_never_raises():
    realtime.publish({"type": "chat_updated", "chatId": 1})

    class DeadLoop:
        def call_soon_threadsafe(self, *a):
            raise RuntimeError("Event loop is closed")

    sub = realtime.Subscriber.__new__(realtime.Subscriber)
    sub.loop = DeadLoop()
    realtime._subscribers.add(sub)
    realtime.chat_updated(5)
    assert sub not in realtime._subscribers


# --- Слушатель: разбор кадров ----------------------------------------------


@pytest.fixture
def published(monkeypatch):
    events = []
    monkeypatch.setattr(realtime, "publish", events.append)
    return events


def test_ping_is_answered_with_same_seq(published):
    replies = []
    listener.FrameHandler(replies.append).handle({"ver": 11, "cmd": 0, "seq": 3, "opcode": 1, "payload": None})
    assert replies == [{"ver": 11, "cmd": 1, "seq": 3, "opcode": 1, "payload": None}]
    assert published == []


def test_new_message_is_acked_and_published(published):
    replies = []
    listener.FrameHandler(replies.append).handle(
        {"ver": 11, "cmd": 0, "seq": 4, "opcode": 128, "payload": {"chatId": -71, "message": {"id": "m1"}}}
    )
    assert replies == [{"ver": 11, "cmd": 1, "seq": 4, "opcode": 128, "payload": {"chatId": -71, "messageId": "m1"}}]
    assert published == [{"type": "chat_updated", "chatId": -71}]


def test_repeated_or_old_seq_is_skipped(published):
    replies = []
    h = listener.FrameHandler(replies.append)
    frame = {"ver": 11, "cmd": 0, "seq": 10, "opcode": 128, "payload": {"chatId": 1, "message": {"id": "a"}}}
    h.handle(frame)
    h.handle(frame)
    h.handle({**frame, "seq": 9})
    assert len(replies) == 1 and len(published) == 1


def test_responses_and_other_pushes_are_ignored(published):
    replies = []
    h = listener.FrameHandler(replies.append)
    h.handle({"ver": 11, "cmd": 1, "seq": 1, "opcode": 49, "payload": {"messages": []}})
    h.handle({"ver": 11, "cmd": 3, "seq": 2, "opcode": 64, "payload": {"error": "x"}})
    h.handle({"ver": 11, "cmd": 0, "seq": 3, "opcode": 129, "payload": {"chatId": 1}})  # «печатает…»
    assert replies == [] and published == []


# --- Слушатель: цикл и запуск ------------------------------------------------


class _Stop(Exception):
    pass


def test_run_reconnects_and_resyncs_after_failures(monkeypatch, published):
    frames = iter([
        {"ver": 11, "cmd": 0, "seq": 1, "opcode": 128, "payload": {"chatId": 42, "message": {"id": "x"}}},
    ])
    sent = []

    class FakeWs:
        def send(self, raw):
            sent.append(raw)

    class FakeSession:
        ws = FakeWs()
        closed = False

        def _recv(self):
            try:
                return next(frames)
            except StopIteration:
                raise ConnectionError("обрыв") from None

        def close(self):
            FakeSession.closed = True

    attempts = []

    def open_session():
        attempts.append(1)
        if len(attempts) == 1:
            raise ConnectionError("MAX недоступен")
        return FakeSession()

    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            raise _Stop

    monkeypatch.setattr(listener, "_open_session", open_session)
    monkeypatch.setattr(listener.time, "sleep", fake_sleep)
    with pytest.raises(_Stop):
        listener._run()

    assert len(attempts) == 2
    assert published == [{"type": "resync"}, {"type": "chat_updated", "chatId": 42}]
    assert len(sent) == 1  # подтверждение кадра 128
    assert FakeSession.closed
    # первая неудача — 5 с; после успешного открытия пауза снова минимальная
    assert sleeps == [listener.RETRY_MIN_SECONDS, listener.RETRY_MIN_SECONDS]


@pytest.mark.parametrize("token,live", [("", True), ("tok", False)])
def test_listener_not_started_without_token_or_when_disabled(monkeypatch, token, live):
    monkeypatch.setattr(listener, "_started", False)
    monkeypatch.setattr(settings, "max_token", token)
    monkeypatch.setattr(settings, "max_live_updates", live)
    started = []
    monkeypatch.setattr(threading, "Thread", lambda *a, **k: started.append(k) or pytest.fail("не должен стартовать"))
    listener.start_max_listener()
    assert started == []


def test_listen_pings_and_gives_up_on_silence(monkeypatch, published):
    """MAX сам не пингует и закрывает молчащую сессию — слушатель пингует сам,
    а если не приходит ни одного кадра дольше IDLE_TIMEOUT, бросает сессию."""
    clock = [0.0]
    monkeypatch.setattr(listener.time, "monotonic", lambda: clock[0])
    Timeout = listener.max_client.websocket.WebSocketTimeoutException
    pings = []

    class FakeSession:
        ws = None

        def _send(self, opcode, payload):
            pings.append((clock[0], opcode, payload))

        def _recv(self):
            clock[0] += listener.RECV_TIMEOUT
            if clock[0] == 30:  # сервер ответил на первый пинг
                return {"ver": 11, "cmd": 1, "seq": 1, "opcode": 1, "payload": None}
            raise Timeout("тишина")

    with pytest.raises(TimeoutError):
        listener._listen(FakeSession())

    assert pings[0] == (25, 1, {"interactive": False})
    assert all(b - a >= listener.PING_INTERVAL for (a, *_), (b, *_) in zip(pings, pings[1:]))
    # последний кадр в 30 с → сдаётся только после 30 + IDLE_TIMEOUT
    assert clock[0] > 30 + listener.IDLE_TIMEOUT
    assert published == []
