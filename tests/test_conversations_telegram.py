"""Канал Telegram переписки из карточки (0083-e).

Привязка — по ``/start <метка>`` из ссылки-приглашения; сообщения
привязанного личного чата — в ленту; блокировка бота — канал отключён.
Формы Update — по core.telegram.org/bots/api, значения синтетические;
Telegram не вызывается.
"""

import pytest

from app.common.module_access import AccessLevel, Module
from app.conversations import telegram_api, telegram_channel
from app.conversations.models import ChannelKind, ContactChannel
from app.core.config import settings

PERSON = {"id": 501, "is_bot": False, "first_name": "Анна", "last_name": "Землякова"}
CHAT = {"id": 501, "type": "private", "first_name": "Анна"}


@pytest.fixture(autouse=True)
def _telegram(monkeypatch):
    monkeypatch.setattr(settings, "telegram_bot_token", "test-token")
    monkeypatch.setattr(settings, "telegram_webhook_secret", "tg-secret")
    monkeypatch.setattr(telegram_channel, "_username", None)
    monkeypatch.setattr(telegram_api, "get_me", lambda: {"id": 1, "is_bot": True, "username": "DurovTestBot"})
    sent = []

    def fake_send(chat_id, text):
        sent.append((chat_id, text))
        return {"message_id": 900 + len(sent), "date": 1_790_000_100, "chat": CHAT, "text": text}

    monkeypatch.setattr(telegram_api, "send_message", fake_send)
    return sent


def _partner(http):
    return http.post("/api/partners/", json={"category": "LAND_SPECIALIST", "name": "Анна", "city": "Тверь"}).json()["id"]


def _update(update_id, text, chat=CHAT, message_id=None, date=1_790_000_000):
    return {
        "update_id": update_id,
        "message": {"message_id": message_id or update_id, "from": PERSON, "chat": chat, "date": date,
                    "text": text},
    }


def _hook(http, update, secret="tg-secret"):
    headers = {"X-Telegram-Bot-Api-Secret-Token": secret} if secret else {}
    return http.post("/api/conversations/telegram/webhook", json=update, headers=headers)


def _connect(http, db, pid):
    link = http.post(f"/api/conversations/partners/{pid}/invite", json={"channel": "TELEGRAM"}).json()["invite_link"]
    token = db.query(ContactChannel).filter(
        ContactChannel.partner_id == pid, ContactChannel.channel == ChannelKind.TELEGRAM
    ).one().invite_token
    assert link == f"https://t.me/DurovTestBot?start={token}"
    assert _hook(http, _update(1, f"/start {token}")).status_code == 200
    return token


def test_start_by_invite_connects_and_messages_flow(api, make_user, db, _telegram):
    http = api(make_user(Module.CLIENTS, level=AccessLevel.EDIT))
    pid = _partner(http)
    _connect(http, db, pid)

    state = http.get(f"/api/conversations/partners/{pid}").json()
    tg = next(c for c in state["channels"] if c["channel"] == "TELEGRAM")
    assert (tg["status"], tg["connected_name"]) == ("CONNECTED", "Анна Землякова")
    # /start — служебное, в ленту не попадает.
    assert state["messages"] == []

    resp = http.post(f"/api/conversations/partners/{pid}/messages", json={"channel": "TELEGRAM", "text": "Участок свободен?"})
    assert resp.status_code == 201, resp.text
    assert _telegram == [("501", "Участок свободен?")]

    # Ответ — позже отправленного (sendMessage в фикстуре датирован 1_790_000_100).
    _hook(http, _update(2, "Да, свободен", date=1_790_000_200))
    _hook(http, _update(2, "Да, свободен", date=1_790_000_200))  # повтор доставки
    feed = http.get(f"/api/conversations/partners/{pid}").json()["messages"]
    assert [(m["channel"], m["direction"], m["text"]) for m in feed] == [
        ("TELEGRAM", "OUT", "Участок свободен?"),
        ("TELEGRAM", "IN", "Да, свободен"),
    ]


def test_no_max_but_telegram_connected_is_offered(api, make_user, db):
    """Сценарий заказчика: Макса у человека нет → «Пользователь не найден в
    Макс. Доступные каналы: Telegram» и переключение туда."""
    http = api(make_user(Module.CLIENTS, level=AccessLevel.EDIT))
    pid = _partner(http)
    _connect(http, db, pid)

    resp = http.post(f"/api/conversations/partners/{pid}/messages", json={"text": "Здравствуйте"})
    assert resp.status_code == 409
    assert resp.json()["detail"] == "Пользователь не найден в Макс. Доступные каналы: Telegram"
    assert resp.json()["available"] == ["TELEGRAM"]


def test_configured_but_unconnected_telegram_is_offered_for_invite(api, make_user):
    http = api(make_user(Module.CLIENTS, level=AccessLevel.EDIT))
    pid = _partner(http)
    body = http.post(f"/api/conversations/partners/{pid}/messages", json={"text": "Здравствуйте"}).json()
    assert body["available"] == ["TELEGRAM"]


def test_foreign_start_and_group_messages_are_ignored(api, make_user, db):
    http = api(make_user(Module.CLIENTS, level=AccessLevel.EDIT))
    pid = _partner(http)
    http.post(f"/api/conversations/partners/{pid}/invite", json={"channel": "TELEGRAM"})

    _hook(http, _update(1, "/start login_abc123"))
    _hook(http, _update(2, "/start"))
    _hook(http, _update(3, "привет всем", chat={"id": -100, "type": "supergroup", "title": "Стройка"}))
    state = http.get(f"/api/conversations/partners/{pid}").json()
    assert next(c for c in state["channels"] if c["channel"] == "TELEGRAM")["status"] == "INVITED"
    assert state["messages"] == []


def test_blocked_bot_stops_channel(api, make_user, db, monkeypatch):
    http = api(make_user(Module.CLIENTS, level=AccessLevel.EDIT))
    pid = _partner(http)
    _connect(http, db, pid)

    _hook(http, {"update_id": 5, "my_chat_member": {"chat": CHAT, "from": PERSON, "date": 1,
                                                     "old_chat_member": {"status": "member"},
                                                     "new_chat_member": {"status": "kicked"}}})
    resp = http.post(f"/api/conversations/partners/{pid}/messages", json={"channel": "TELEGRAM", "text": "Вы тут?"})
    assert resp.status_code == 409
    assert resp.json()["detail"].startswith("Пользователь отключил бота в Telegram.")


def test_telegram_403_on_send_marks_failed_and_stopped(api, make_user, db, monkeypatch):
    http = api(make_user(Module.CLIENTS, level=AccessLevel.EDIT))
    pid = _partner(http)
    _connect(http, db, pid)

    def blocked(chat_id, text):
        raise telegram_api.TelegramApiError(403, "Telegram отклонил sendMessage: Forbidden: bot was blocked by the user")

    monkeypatch.setattr(telegram_api, "send_message", blocked)
    resp = http.post(f"/api/conversations/partners/{pid}/messages", json={"channel": "TELEGRAM", "text": "Здравствуйте"})
    assert resp.status_code == 502
    state = http.get(f"/api/conversations/partners/{pid}").json()
    assert state["messages"][0]["delivery"] == "FAILED"
    assert next(c for c in state["channels"] if c["channel"] == "TELEGRAM")["status"] == "STOPPED"


def test_webhook_rejects_wrong_or_missing_secret(api, make_user, db):
    http = api(make_user(Module.CLIENTS, level=AccessLevel.EDIT))
    pid = _partner(http)
    http.post(f"/api/conversations/partners/{pid}/invite", json={"channel": "TELEGRAM"})
    token = db.query(ContactChannel).one().invite_token

    assert _hook(http, _update(1, f"/start {token}"), secret=None).status_code == 403
    assert _hook(http, _update(1, f"/start {token}"), secret="wrong").status_code == 403
    assert db.query(ContactChannel).one().status.value == "INVITED"


def test_webhook_closed_when_secret_not_configured(api, make_user, monkeypatch):
    monkeypatch.setattr(settings, "telegram_webhook_secret", "")
    http = api(make_user(Module.CLIENTS, level=AccessLevel.EDIT))
    assert _hook(http, _update(1, "привет"), secret="").status_code == 403
