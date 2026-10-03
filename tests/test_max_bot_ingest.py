"""События MAX-бота → max_bot_chats / max_bot_messages (0082-a).

Форма Update — по dev.max.ru/docs-api/objects/Update; значения синтетические.
"""

import pytest

from app.core.config import settings
from app.max import bot_api, ingest
from app.max.models import MaxBotChat, MaxBotMessage

BOT_ID = 1000
USER = {"user_id": 42, "first_name": "Иван", "last_name": "Петров", "is_bot": False}


@pytest.fixture(autouse=True)
def _bot(monkeypatch):
    monkeypatch.setattr(ingest, "bot_user_id", lambda: BOT_ID)
    monkeypatch.setattr(bot_api, "get_chat", lambda chat_id: {"chat_id": chat_id, "title": "Стройка Иванова"})


def _msg(mid, text="привет", chat_id=555, chat_type="dialog", sender=USER, ts=1_790_000_000_000, attachments=None):
    return {
        "update_type": "message_created",
        "timestamp": ts,
        "message": {
            "sender": sender,
            "recipient": {"chat_id": chat_id, "chat_type": chat_type},
            "timestamp": ts,
            "body": {"mid": mid, "seq": 1, "text": text, "attachments": attachments},
        },
    }


def test_incoming_message_creates_dialog_and_counts_unread(db):
    ingest.handle_update(db, _msg("mid.1"))

    chat = db.get(MaxBotChat, 555)
    assert chat.type == "dialog"
    assert chat.title == "Иван Петров"
    assert chat.dialog_user_id == 42
    assert chat.unread == 1
    msg = db.get(MaxBotMessage, "mid.1")
    assert msg.text == "привет" and msg.is_outgoing is False and msg.sender_name == "Иван Петров"


def test_repeated_delivery_is_idempotent(db):
    ingest.handle_update(db, _msg("mid.1"))
    ingest.handle_update(db, _msg("mid.1"))

    assert db.query(MaxBotMessage).count() == 1
    assert db.get(MaxBotChat, 555).unread == 1


def test_bot_own_message_is_outgoing_and_not_unread(db):
    ingest.handle_update(db, _msg("mid.2", sender={"user_id": BOT_ID, "first_name": "Марина", "is_bot": True}))

    assert db.get(MaxBotMessage, "mid.2").is_outgoing is True
    assert db.get(MaxBotChat, 555).unread == 0


def test_edit_and_remove(db):
    ingest.handle_update(db, _msg("mid.3", text="было"))
    edited = _msg("mid.3", text="стало")
    edited["update_type"] = "message_edited"
    ingest.handle_update(db, edited)
    ingest.handle_update(db, {"update_type": "message_removed", "message_id": "mid.3", "chat_id": 555})

    msg = db.get(MaxBotMessage, "mid.3")
    assert msg.text == "стало" and msg.deleted is True
    assert db.get(MaxBotChat, 555).unread == 1


def test_bot_added_to_group_takes_title_and_removal_marks_chat(db):
    ingest.handle_update(db, {"update_type": "bot_added", "timestamp": 1, "chat_id": -900, "user": USER, "is_channel": False})
    chat = db.get(MaxBotChat, -900)
    assert chat.type == "chat" and chat.title == "Стройка Иванова" and chat.status == "active"

    ingest.handle_update(db, {"update_type": "bot_removed", "timestamp": 2, "chat_id": -900, "user": USER})
    assert db.get(MaxBotChat, -900).status == "removed"


def test_bot_started_registers_dialog_and_unknown_update_is_ignored(db):
    ingest.handle_update(db, {"update_type": "bot_started", "timestamp": 5, "chat_id": 777, "user": USER})
    ingest.handle_update(db, {"update_type": "something_new", "chat_id": 1})

    chat = db.get(MaxBotChat, 777)
    assert chat.type == "dialog" and chat.dialog_user_id == 42 and chat.title == "Иван Петров"
    assert db.query(MaxBotChat).count() == 1


def test_broken_update_does_not_drop_the_batch(db):
    ingest.handle_updates(db, [{"update_type": "message_created", "message": "не объект"}, _msg("mid.4")])

    assert db.get(MaxBotMessage, "mid.4") is not None


def test_webhook_rejects_missing_or_wrong_secret(monkeypatch, api, make_user, db):
    monkeypatch.setattr(settings, "max_webhook_secret", "s3cret-value")
    client = api(make_user())

    assert client.post("/api/max/webhook", json=_msg("mid.5")).status_code == 403
    assert client.post("/api/max/webhook", json=_msg("mid.5"), headers={"X-Max-Bot-Api-Secret": "nope"}).status_code == 403
    assert db.query(MaxBotMessage).count() == 0


def test_webhook_without_configured_secret_rejects_everything(monkeypatch, api, make_user, db):
    monkeypatch.setattr(settings, "max_webhook_secret", "")
    client = api(make_user())

    assert client.post("/api/max/webhook", json=_msg("mid.6"), headers={"X-Max-Bot-Api-Secret": ""}).status_code == 403


def test_webhook_with_secret_stores_message(monkeypatch, api, make_user, db):
    monkeypatch.setattr(settings, "max_webhook_secret", "s3cret-value")
    client = api(make_user())

    resp = client.post("/api/max/webhook", json=_msg("mid.7"), headers={"X-Max-Bot-Api-Secret": "s3cret-value"})
    assert resp.status_code == 200
    assert db.get(MaxBotMessage, "mid.7").text == "привет"


def test_client_without_token_is_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "max_bot_token", "")
    with pytest.raises(bot_api.BotNotConfigured):
        bot_api.get_me()


def test_bot_id_lookup_backs_off_after_failure(monkeypatch):
    """Недоступный MAX не опрашивается на каждом событии — пауза после сбоя."""
    monkeypatch.undo()  # снять автоподмену bot_user_id из фикстуры _bot
    calls = []

    def failing_me():
        calls.append(1)
        raise bot_api.BotApiError(None, None, "MAX не ответил")

    monkeypatch.setattr(bot_api, "get_me", failing_me)
    monkeypatch.setattr(ingest, "_bot_id", None)
    monkeypatch.setattr(ingest, "_bot_id_failed_at", None)

    assert ingest.bot_user_id() is None
    assert ingest.bot_user_id() is None
    assert calls == [1]
