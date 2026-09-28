"""Переписка из карточки через MAX-бота (0083-d).

Бот не пишет первым и не ищет по телефону, поэтому чат привязывается к
карточке по ссылке-приглашению с меткой: человек нажимает «Начать» →
``bot_started`` с этой меткой в ``payload``. Формы событий — по
dev.max.ru/docs-api, значения синтетические; MAX не вызывается.
"""

import pytest

from app.common.module_access import AccessLevel, Module
from app.conversations import max_channel
from app.conversations.models import ContactChannel, ConversationMessage
from app.core.config import settings
from app.max import bot_api, ingest

BOT_ID = 1000
PERSON = {"user_id": 42, "first_name": "Пётр", "last_name": "Риэлторов", "is_bot": False}
CHAT_ID = 777


@pytest.fixture(autouse=True)
def _max_bot(monkeypatch):
    monkeypatch.setattr(settings, "max_bot_token", "test-token")
    monkeypatch.setattr(max_channel, "_username", None)
    monkeypatch.setattr(bot_api, "get_me", lambda: {"user_id": BOT_ID, "username": "durov_test_bot"})
    monkeypatch.setattr(ingest, "bot_user_id", lambda: BOT_ID)
    sent = []

    def fake_send(chat_id, text, attachments=None, notify=True):
        sent.append((chat_id, text))
        return {
            "sender": {"user_id": BOT_ID, "is_bot": True},
            "recipient": {"chat_id": chat_id, "chat_type": "dialog"},
            "timestamp": 1_790_000_100_000,
            "body": {"mid": f"mid.out.{len(sent)}", "text": text},
        }

    monkeypatch.setattr(bot_api, "send_message", fake_send)
    return sent


def _partner(http):
    resp = http.post("/api/partners/", json={"category": "REALTOR", "name": "Пётр Риэлторов", "city": "Москва"})
    return resp.json()["id"]


def _started(payload, chat_id=CHAT_ID):
    return {"update_type": "bot_started", "timestamp": 1_790_000_000_000, "chat_id": chat_id,
            "user": PERSON, "payload": payload}


def _incoming(mid, text, chat_id=CHAT_ID):
    return {
        "update_type": "message_created",
        "timestamp": 1_790_000_200_000,
        "message": {
            "sender": PERSON,
            "recipient": {"chat_id": chat_id, "chat_type": "dialog"},
            "timestamp": 1_790_000_200_000,
            "body": {"mid": mid, "seq": 1, "text": text},
        },
    }


def _token(db, partner_id):
    return db.query(ContactChannel).filter(ContactChannel.partner_id == partner_id).one().invite_token


def test_send_without_max_says_user_not_found(api, make_user):
    http = api(make_user(Module.CLIENTS, level=AccessLevel.EDIT))
    pid = _partner(http)

    resp = http.post(f"/api/conversations/partners/{pid}/messages", json={"text": "Здравствуйте"})
    assert resp.status_code == 409
    body = resp.json()
    assert body["code"] == "channel_unavailable"
    assert body["detail"].startswith("Пользователь не найден в Макс.")
    # Telegram/WhatsApp на сервере пока не настроены — их не предлагаем.
    assert body["available"] == []
    assert "ссылку-приглашение" in body["detail"]
    assert http.get(f"/api/conversations/partners/{pid}").json()["messages"] == []


def test_invite_link_is_stable_deeplink(api, make_user, db):
    http = api(make_user(Module.CLIENTS, level=AccessLevel.EDIT))
    pid = _partner(http)

    first = http.post(f"/api/conversations/partners/{pid}/invite", json={"channel": "MAX"}).json()
    again = http.post(f"/api/conversations/partners/{pid}/invite", json={"channel": "MAX"}).json()
    token = _token(db, pid)
    assert first["invite_link"] == f"https://max.ru/durov_test_bot?start={token}"
    assert again["invite_link"] == first["invite_link"]
    assert first["status"] == "INVITED"
    assert token.startswith("dos_") and len(token) <= 40


def test_unconfigured_channel_cannot_invite(api, make_user):
    http = api(make_user(Module.CLIENTS, level=AccessLevel.EDIT))
    pid = _partner(http)
    assert http.post(f"/api/conversations/partners/{pid}/invite", json={"channel": "TELEGRAM"}).status_code == 503


def test_start_by_invite_connects_and_conversation_flows(api, make_user, db, _max_bot):
    http = api(make_user(Module.CLIENTS, level=AccessLevel.EDIT))
    pid = _partner(http)
    http.post(f"/api/conversations/partners/{pid}/invite", json={"channel": "MAX"})

    ingest.handle_update(db, _started(_token(db, pid)))
    state = http.get(f"/api/conversations/partners/{pid}").json()
    max_state = next(c for c in state["channels"] if c["channel"] == "MAX")
    assert max_state["status"] == "CONNECTED"
    assert max_state["connected_name"] == "Пётр Риэлторов"

    sent = http.post(f"/api/conversations/partners/{pid}/messages", json={"text": "  Условия: 3%  "})
    assert sent.status_code == 201, sent.text
    assert sent.json()["author_name"] == "Тестовый сотрудник"
    assert _max_bot == [(CHAT_ID, "Условия: 3%")]

    ingest.handle_update(db, _incoming("mid.in.1", "Согласен"))
    feed = http.get(f"/api/conversations/partners/{pid}").json()["messages"]
    assert [(m["direction"], m["text"]) for m in feed] == [("OUT", "Условия: 3%"), ("IN", "Согласен")]


def test_repeated_events_do_not_duplicate(api, make_user, db):
    http = api(make_user(Module.CLIENTS, level=AccessLevel.EDIT))
    pid = _partner(http)
    http.post(f"/api/conversations/partners/{pid}/invite", json={"channel": "MAX"})
    ingest.handle_update(db, _started(_token(db, pid)))

    ingest.handle_update(db, _incoming("mid.in.1", "Согласен"))
    ingest.handle_update(db, _incoming("mid.in.1", "Согласен"))
    assert db.query(ConversationMessage).count() == 1

    # Собственное сообщение бота, отправленное из карточки, вернулось событием — не дубль.
    http.post(f"/api/conversations/partners/{pid}/messages", json={"text": "Отлично"})
    echo = _incoming("mid.out.1", "Отлично")
    echo["message"]["sender"] = {"user_id": BOT_ID, "is_bot": True}
    ingest.handle_update(db, echo)
    assert db.query(ConversationMessage).count() == 2


def test_unknown_payload_and_unbound_chat_are_ignored(api, make_user, db):
    http = api(make_user(Module.CLIENTS, level=AccessLevel.EDIT))
    pid = _partner(http)
    http.post(f"/api/conversations/partners/{pid}/invite", json={"channel": "MAX"})

    ingest.handle_update(db, _started("login_abc"))
    ingest.handle_update(db, _started(None))
    ingest.handle_update(db, _incoming("mid.x", "чужой чат", chat_id=999))
    state = http.get(f"/api/conversations/partners/{pid}").json()
    assert next(c for c in state["channels"] if c["channel"] == "MAX")["status"] == "INVITED"
    assert state["messages"] == []


def test_stopped_bot_blocks_sending(api, make_user, db):
    http = api(make_user(Module.CLIENTS, level=AccessLevel.EDIT))
    pid = _partner(http)
    http.post(f"/api/conversations/partners/{pid}/invite", json={"channel": "MAX"})
    ingest.handle_update(db, _started(_token(db, pid)))

    ingest.handle_update(db, {"update_type": "bot_stopped", "timestamp": 1, "chat_id": CHAT_ID, "user": PERSON})
    resp = http.post(f"/api/conversations/partners/{pid}/messages", json={"text": "Вы здесь?"})
    assert resp.status_code == 409
    assert resp.json()["detail"].startswith("Пользователь отключил бота в Макс.")


def test_max_rejection_is_kept_as_failed(api, make_user, db, monkeypatch):
    http = api(make_user(Module.CLIENTS, level=AccessLevel.EDIT))
    pid = _partner(http)
    http.post(f"/api/conversations/partners/{pid}/invite", json={"channel": "MAX"})
    ingest.handle_update(db, _started(_token(db, pid)))

    def denied(*_a, **_k):
        raise bot_api.BotApiError(403, "chat.denied", "MAX отклонил запрос (403): chat.denied")

    monkeypatch.setattr(bot_api, "send_message", denied)
    resp = http.post(f"/api/conversations/partners/{pid}/messages", json={"text": "Здравствуйте"})
    assert resp.status_code == 502
    state = http.get(f"/api/conversations/partners/{pid}").json()
    assert [(m["delivery"], m["text"]) for m in state["messages"]] == [("FAILED", "Здравствуйте")]
    assert next(c for c in state["channels"] if c["channel"] == "MAX")["status"] == "STOPPED"


def test_client_card_has_its_own_conversation(api, make_user, db):
    http = api(make_user(Module.CLIENTS, level=AccessLevel.EDIT))
    client = http.post(
        "/api/clients/",
        json={"full_name": "Иван Покупателев", "phone": "+70000000000", "email": "i@example.com"},
    ).json()
    link = http.post(f"/api/conversations/clients/{client['id']}/invite", json={"channel": "MAX"}).json()
    assert link["invite_link"].startswith("https://max.ru/durov_test_bot?start=dos_")
    assert http.get("/api/conversations/clients/999999").status_code == 404


def test_access_view_reads_edit_writes(api, make_user):
    editor = api(make_user(Module.CLIENTS, level=AccessLevel.EDIT))
    pid = _partner(editor)
    viewer = api(make_user(Module.CLIENTS, level=AccessLevel.VIEW))
    assert viewer.get(f"/api/conversations/partners/{pid}").status_code == 200
    assert viewer.post(f"/api/conversations/partners/{pid}/invite", json={"channel": "MAX"}).status_code == 403
    assert viewer.post(f"/api/conversations/partners/{pid}/messages", json={"text": "x"}).status_code == 403
    outsider = api(make_user(Module.WAREHOUSE, level=AccessLevel.FULL))
    assert outsider.get(f"/api/conversations/partners/{pid}").status_code == 403
