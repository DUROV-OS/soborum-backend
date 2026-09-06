"""Тесты Telegram-моста: разбор апдейтов, словарь алиасов, диплинк-логин и
ежедневный ingest (без реальных вызовов Telegram/Anthropic)."""

from datetime import datetime, timedelta, timezone

import pytest

from app.core.config import settings
from app.telegram import ingest as tg_ingest
from app.telegram import service as tg_service
from app.telegram.aliases import ALIAS_TO_USER_ID, resolve_user_id
from app.telegram.models import TelegramAccountLink, TelegramLoginToken, TelegramMessage

WATCHED_CHAT = "-1002500000000"


@pytest.fixture(autouse=True)
def _telegram_env(monkeypatch):
    monkeypatch.setattr(settings, "telegram_bot_token", "test:token", raising=False)
    monkeypatch.setattr(settings, "telegram_chat_id", WATCHED_CHAT, raising=False)
    monkeypatch.setattr(tg_service, "_bot_username_cache", "durov_os_bot", raising=False)
    yield


def _text_update(update_id, message_id, text, *, uid=555, username="petrov", ts=None):
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "date": int((ts or datetime.now(timezone.utc)).timestamp()),
            "chat": {"id": int(WATCHED_CHAT), "type": "supergroup"},
            "from": {"id": uid, "first_name": "Иван", "last_name": "Петров", "username": username},
            "text": text,
        },
    }


# --------------------------------------------------------- разбор апдейтов --

def test_group_text_message_is_archived(db):
    tg_service.process_update(db, _text_update(10, 1, "Договор с Ивановым подписан"), tg=None)

    rows = db.query(TelegramMessage).all()
    assert len(rows) == 1
    assert rows[0].chat_id == WATCHED_CHAT
    assert rows[0].kind == "text"
    assert rows[0].text == "Договор с Ивановым подписан"
    assert rows[0].sender_name == "Иван Петров"
    assert db.get(type(rows[0]), rows[0].id).update_id == 10


def test_duplicate_message_is_not_stored_twice(db):
    tg_service.process_update(db, _text_update(10, 1, "первое"), tg=None)
    tg_service.process_update(db, _text_update(11, 1, "первое (ретрансляция)"), tg=None)
    assert db.query(TelegramMessage).count() == 1


def test_offset_advances_even_for_foreign_chats(db):
    foreign = _text_update(42, 7, "не наш чат")
    foreign["message"]["chat"]["id"] = -1  # другой чат
    tg_service.process_update(db, foreign, tg=None)

    assert db.query(TelegramMessage).count() == 0
    assert tg_service.get_offset(db) == 43  # last_update_id 42 + 1


def test_photo_message_classified_without_download(db):
    update = {
        "update_id": 5,
        "message": {
            "message_id": 3,
            "date": int(datetime.now(timezone.utc).timestamp()),
            "chat": {"id": int(WATCHED_CHAT), "type": "supergroup"},
            "from": {"id": 1, "first_name": "А", "username": "a"},
            "caption": "фото площадки",
            "photo": [
                {"file_id": "small", "file_unique_id": "u1", "file_size": 100},
                {"file_id": "big", "file_unique_id": "u2", "file_size": 9000},
            ],
        },
    }
    tg_service.process_update(db, update, tg=None)  # tg=None → без скачивания

    row = db.query(TelegramMessage).one()
    assert row.kind == "photo"
    assert row.text == "фото площадки"
    assert row.file_unique_id == "u2"
    assert row.file_asset_id is None


# ------------------------------------------------------ словарь алиасов --

def test_resolve_user_id_prefers_db_link_then_static_dict(db, make_user, monkeypatch):
    user = make_user()
    # 1. явная привязка
    tg_service.link_account(db, tg_user_id="777", user_id=user.id, tg_username="marina")
    db.commit()
    assert resolve_user_id(db, "777", "marina") == user.id

    # 2. статический словарь по @username, если привязки нет
    other = make_user()
    monkeypatch.setitem(ALIAS_TO_USER_ID, "glebov", other.id)
    assert resolve_user_id(db, "999", "glebov") == other.id
    assert resolve_user_id(db, "999", "unknown") is None


# --------------------------------------------------- диплинк-логин --

def test_deep_link_token_roundtrip_links_account(db, make_user):
    user = make_user()
    token = tg_service.create_login_token(db, user)
    assert token.consumed_at is None

    linked = tg_service.consume_login_token(db, token.token, tg_user_id="12345", tg_username="ivan")
    assert linked is not None and linked.id == user.id

    link = db.query(TelegramAccountLink).filter_by(tg_user_id="12345").one()
    assert link.user_id == user.id and link.linked_at is not None

    # повторное использование не проходит
    assert tg_service.consume_login_token(db, token.token, tg_user_id="12345", tg_username="ivan") is None


def test_expired_login_token_rejected(db, make_user):
    user = make_user()
    token = tg_service.create_login_token(db, user)
    token.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db.commit()
    assert tg_service.consume_login_token(db, token.token, tg_user_id="1", tg_username=None) is None


def test_private_start_with_login_payload_binds_and_replies(db, make_user):
    user = make_user()
    token = tg_service.create_login_token(db, user)

    sent = []

    class FakeTG:
        def send_message(self, chat_id, text, **kw):
            sent.append((chat_id, text))

    update = {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "date": int(datetime.now(timezone.utc).timestamp()),
            "chat": {"id": 12345, "type": "private"},
            "from": {"id": 12345, "first_name": "Иван", "username": "ivan"},
            "text": f"/start login_{token.token}",
        },
    }
    tg_service.process_update(db, update, tg=FakeTG())

    assert db.query(TelegramAccountLink).filter_by(tg_user_id="12345").one().user_id == user.id
    assert sent and "привязан" in sent[0][1]


def test_deep_link_endpoint_returns_tme_url(db, api, make_user):
    resp = api(make_user()).post("/api/telegram/login/deep-link")
    assert resp.status_code == 200
    body = resp.json()
    assert body["url"].startswith("https://t.me/durov_os_bot?start=login_")
    assert body["token"] in body["url"]


# ------------------------------------------------------------ ingest --

def test_ingest_skips_when_no_messages(db, monkeypatch):
    from app.jobs.dates import yesterday

    result = tg_ingest.ingest_day(db, yesterday())
    assert result.status == "skipped"


def test_ingest_calls_kb_agent_and_marks_messages(db, make_user, monkeypatch):
    from app.jobs.dates import day_bounds_utc, yesterday

    day = yesterday()
    since, _ = day_bounds_utc(day)
    ts = since + timedelta(hours=10)

    tg_service.process_update(db, _text_update(1, 1, "Игорь просил смету по DH-96", ts=ts), tg=None)
    tg_service.process_update(db, _text_update(2, 2, "новый лид: Сидоров, +7999", ts=ts), tg=None)

    captured = {}

    def fake_agent(_db, *, system, user_content, allow_write=True, max_tokens=8192):
        captured["system"] = system
        captured["user_content"] = user_content
        return "Добавил 2 записи в 01_Inbox/Daily."

    monkeypatch.setattr(tg_ingest, "run_kb_agent", fake_agent)

    result = tg_ingest.ingest_day(db, day)
    assert result.status == "ok"
    assert "2 сообщени" in result.detail
    assert day.isoformat() in captured["system"]
    assert "Игорь просил смету" in captured["user_content"][0]["text"]

    # помечены как разнесённые → повторный запуск ничего не делает
    assert all(m.ingested_into_kb_at is not None for m in db.query(TelegramMessage).all())
    assert tg_ingest.ingest_day(db, day).status == "skipped"
