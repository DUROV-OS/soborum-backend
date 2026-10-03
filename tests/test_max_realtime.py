"""WS /api/max/ws — события чатов MAX в реальном времени (0091).

Форма Update — по dev.max.ru/docs-api/objects/Update; значения синтетические.
"""

import time

import pytest
from starlette.websockets import WebSocketDisconnect

from app.core.config import settings
from app.core.security import create_access_token
from app.max import bot_api, ingest, realtime
from app.max import router as max_router
from app.max import service as max_service

BOT_ID = 1000
USER = {"user_id": 42, "first_name": "Иван", "last_name": "Петров", "is_bot": False}
SECRET = "test-webhook-secret"


@pytest.fixture(autouse=True)
def _bot(monkeypatch):
    monkeypatch.setattr(ingest, "bot_user_id", lambda: BOT_ID)
    monkeypatch.setattr(max_service, "bot_user_id", lambda: BOT_ID)
    monkeypatch.setattr(bot_api, "get_chat", lambda chat_id: {"chat_id": chat_id, "title": "Стройка"})
    monkeypatch.setattr(settings, "max_bot_token", "test-token")
    monkeypatch.setattr(settings, "max_webhook_secret", SECRET)


def _update(mid, chat_id=555, text="привет", sender=USER, kind="message_created"):
    return {
        "update_type": kind,
        "timestamp": 1_790_000_000_000,
        "message": {
            "sender": sender,
            "recipient": {"chat_id": chat_id, "chat_type": "dialog"},
            "timestamp": 1_790_000_000_000,
            "body": {"mid": mid, "seq": 1, "text": text},
        },
    }


def _wait_no_subscribers():
    for _ in range(50):
        if not realtime._subscribers:
            return
        time.sleep(0.02)
    pytest.fail("подписчик не отписался после закрытия соединения")


def _connect_authed(client, user):
    ws = client.websocket_connect("/api/max/ws")
    conn = ws.__enter__()
    conn.send_json({"type": "auth", "token": create_access_token(str(user.id))})
    assert conn.receive_json() == {"type": "ready"}
    return ws, conn


def test_incoming_message_via_webhook_is_pushed(api, make_user):
    user = make_user()
    client = api(user)
    ws, conn = _connect_authed(client, user)
    try:
        r = client.post("/api/max/webhook", json=_update("mid.1"), headers={"X-Max-Bot-Api-Secret": SECRET})
        assert r.status_code == 200
        assert conn.receive_json() == {"type": "chat_updated", "chatId": 555}
    finally:
        ws.__exit__(None, None, None)
    _wait_no_subscribers()


def test_removed_message_is_pushed_with_its_chat(api, make_user, db):
    ingest.handle_update(db, _update("mid.7", chat_id=777))
    user = make_user()
    client = api(user)
    ws, conn = _connect_authed(client, user)
    try:
        r = client.post(
            "/api/max/webhook",
            json={"update_type": "message_removed", "message_id": "mid.7", "timestamp": 1},
            headers={"X-Max-Bot-Api-Secret": SECRET},
        )
        assert r.status_code == 200
        assert conn.receive_json() == {"type": "chat_updated", "chatId": 777}
    finally:
        ws.__exit__(None, None, None)


def test_sent_message_is_pushed_to_other_tabs(monkeypatch, api, make_user):
    def fake_send(chat_id, text, attachments=None, notify=True):
        return {"message": {
            "sender": {"user_id": BOT_ID, "is_bot": True, "first_name": "Durov OS"},
            "recipient": {"chat_id": chat_id, "chat_type": "dialog"},
            "timestamp": 1_790_000_000_500,
            "body": {"mid": "mid.out", "seq": 2, "text": text},
        }}

    monkeypatch.setattr(bot_api, "send_message", fake_send)
    user = make_user()
    client = api(user)
    ws, conn = _connect_authed(client, user)
    try:
        r = client.post("/api/max/messages", json={"chat_id": 555, "text": "ответ"})
        assert r.status_code == 201
        assert conn.receive_json() == {"type": "chat_updated", "chatId": 555}
    finally:
        ws.__exit__(None, None, None)


def test_rejected_webhook_pushes_nothing(api, make_user):
    user = make_user()
    client = api(user)
    ws, conn = _connect_authed(client, user)
    try:
        r = client.post("/api/max/webhook", json=_update("mid.2"), headers={"X-Max-Bot-Api-Secret": "wrong-secret"})
        assert r.status_code == 403
        # следующее событие — уже от честного webhook, отклонённый ничего не разослал
        client.post("/api/max/webhook", json=_update("mid.3", chat_id=999), headers={"X-Max-Bot-Api-Secret": SECRET})
        assert conn.receive_json() == {"type": "chat_updated", "chatId": 999}
    finally:
        ws.__exit__(None, None, None)


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


def test_publish_without_subscribers_and_with_dead_loop_never_raises():
    realtime.publish({"type": "chat_updated", "chatId": 1})

    class DeadLoop:
        def call_soon_threadsafe(self, *a):
            raise RuntimeError("Event loop is closed")

    sub = realtime.Subscriber.__new__(realtime.Subscriber)
    sub.loop = DeadLoop()
    realtime._subscribers.add(sub)
    realtime.chat_updated(5)
    assert sub not in realtime._subscribers
