"""Эндпоинты /api/max поверх бота и БД (0082-b): форма ответа прежняя
(frontend/src/max/types.ts), Bot API подменён — реальный MAX не трогаем.
Значения синтетические, форма Message — по dev.max.ru/docs-api.
"""

import pytest

from app.common.module_access import Module
from app.core.config import settings
from app.max import bot_api, ingest
from app.max import service as max_service
from app.max.models import MaxBotChat, MaxBotMessage

BOT_ID = 1000
USER = {"user_id": 42, "first_name": "Иван", "last_name": "Петров", "is_bot": False}
BOT = {"user_id": BOT_ID, "first_name": "Марина", "is_bot": True}


@pytest.fixture(autouse=True)
def _bot(monkeypatch):
    monkeypatch.setattr(ingest, "bot_user_id", lambda: BOT_ID)
    monkeypatch.setattr(max_service, "bot_user_id", lambda: BOT_ID)
    monkeypatch.setattr(bot_api, "get_chat", lambda chat_id: {"chat_id": chat_id, "title": "Стройка"})
    monkeypatch.setattr(settings, "max_bot_token", "test-token")
    monkeypatch.setattr(max_service, "ATTACHMENT_RETRY_DELAY", 0)


def _message(mid, text="привет", chat_id=555, chat_type="dialog", sender=USER, ts=1_790_000_000_000,
             attachments=None):
    return {
        "sender": sender,
        "recipient": {"chat_id": chat_id, "chat_type": chat_type},
        "timestamp": ts,
        "body": {"mid": mid, "seq": 1, "text": text, "attachments": attachments},
    }


def _ingest(db, message):
    ingest.handle_update(db, {"update_type": "message_created", "message": message})


def test_chat_list_from_db_with_last_message(api, make_user, db):
    _ingest(db, _message("mid.1", "первое", ts=1))
    _ingest(db, _message("mid.2", "второе", ts=2))
    _ingest(db, _message("mid.3", "в группе", chat_id=-900, chat_type="chat", ts=3))
    db.add(MaxBotChat(chat_id=777, type="dialog", title="Ушёл", status="removed", unread=0, last_event_time=9))
    db.commit()

    res = api(make_user(Module.CLIENTS)).get("/api/max/chats")
    assert res.status_code == 200, res.text
    body = res.json()
    assert [c["id"] for c in body["chats"]] == [-900, 555]
    dialog = body["chats"][1]
    assert dialog["type"] == "DIALOG"
    assert dialog["title"] == "Иван Петров"
    assert dialog["unread"] == 2
    assert dialog["lastMessage"]["text"] == "второе"
    assert body["chats"][0]["type"] == "CHAT"
    assert body["chats"][0]["title"] == "Стройка"


def test_chat_history_resets_unread_and_maps_attachments(api, make_user, db):
    _ingest(db, _message("mid.1", "фото и файл", attachments=[
        {"type": "image", "payload": {"photo_id": 12345678901234567, "url": "https://i.max.test/p.jpg"}},
        {"type": "file", "payload": {"url": "https://f.max.test/s.pdf"}, "filename": "смета.pdf", "size": 2048},
        {"type": "video", "payload": {"token": "vt", "url": "https://v.max.test/w"},
         "thumbnail": {"url": "https://v.max.test/t.jpg"}, "duration": 7},
        {"type": "audio", "payload": {"url": "https://a.max.test/a.ogg"}},
        {"type": "share", "payload": {"url": "https://durov.house"}, "title": "Durov House"},
        {"type": "location", "latitude": 55.7, "longitude": 37.6},
    ]))

    worker = api(make_user(Module.CLIENTS))
    res = worker.get("/api/max/chats/555")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["viewerId"] == str(BOT_ID)
    assert body["isGroup"] is False
    msg = body["messages"][0]
    assert msg["isOutgoing"] is False
    assert msg["senderName"] == "Иван Петров"
    assert msg["attaches"] == [
        {"type": "PHOTO", "baseUrl": "https://i.max.test/p.jpg", "photoId": "12345678901234567"},
        {"type": "FILE", "name": "смета.pdf", "size": 2048, "fileId": "1"},
        {"type": "VIDEO", "videoId": "2", "thumbnail": "https://v.max.test/t.jpg", "duration": 7000},
        {"type": "AUDIO", "audioId": "3"},
        {"type": "SHARE", "url": "https://durov.house", "title": "Durov House"},
        {"type": "UNSUPPORTED"},
    ]
    assert db.get(MaxBotChat, 555).unread == 0


def test_history_limit_and_backward(api, make_user, db):
    for i in range(1, 6):
        _ingest(db, _message(f"mid.{i}", f"m{i}", ts=i))
    worker = api(make_user(Module.CLIENTS))

    last_two = worker.get("/api/max/chats/555", params={"limit": 2}).json()["messages"]
    assert [m["text"] for m in last_two] == ["m4", "m5"]
    more = worker.get("/api/max/chats/555", params={"limit": 2, "backward": 2}).json()["messages"]
    assert [m["text"] for m in more] == ["m2", "m3", "m4", "m5"]


def test_unknown_chat_is_404(api, make_user):
    res = api(make_user(Module.CLIENTS)).get("/api/max/chats/123")
    assert res.status_code == 404
    assert res.json()["detail"] == "Бот не состоит в этом чате"


def test_send_text_is_stored_as_outgoing(monkeypatch, api, make_user, db):
    _ingest(db, _message("mid.1"))
    sent = {}

    def fake_send(chat_id, text, attachments=None, notify=True):
        sent.update(chat_id=chat_id, text=text, attachments=attachments)
        return _message("mid.out", text, sender=BOT, ts=1_790_000_000_500)

    monkeypatch.setattr(bot_api, "send_message", fake_send)
    worker = api(make_user(Module.CLIENTS))

    res = worker.post("/api/max/messages", json={"chat_id": 555, "text": "ответ 1"})
    assert res.status_code == 201, res.text
    assert sent == {"chat_id": 555, "text": "ответ 1", "attachments": None}
    assert res.json()["message"]["isOutgoing"] is True
    row = db.get(MaxBotMessage, "mid.out")
    assert row.is_outgoing and row.text == "ответ 1"
    assert db.get(MaxBotChat, 555).unread == 1  # своё сообщение не непрочитанное


def test_send_file_uploads_then_retries_until_ready(monkeypatch, api, make_user, db):
    _ingest(db, _message("mid.1"))
    uploaded, attempts = [], []
    monkeypatch.setattr(bot_api, "get_upload_url", lambda kind: {"url": f"https://upload.max.test/{kind}"})
    monkeypatch.setattr(
        bot_api, "upload",
        lambda url, data, filename, ct: uploaded.append((url, data, filename)) or {"token": "ftok"},
    )

    def fake_send(chat_id, text, attachments=None, notify=True):
        attempts.append(attachments)
        if len(attempts) < 3:
            raise bot_api.BotApiError(400, "attachment.not.ready", "not ready")
        return _message("mid.f", text, sender=BOT, attachments=[
            {"type": "file", "payload": {"url": "https://f.max.test/x.pdf"}, "filename": "x.pdf", "size": 3},
        ])

    monkeypatch.setattr(bot_api, "send_message", fake_send)
    worker = api(make_user(Module.CLIENTS))

    res = worker.post(
        "/api/max/messages/attachment",
        data={"chat_id": "555", "text": ""},
        files={"file": ("x.pdf", b"PDF", "application/pdf")},
    )
    assert res.status_code == 201, res.text
    assert uploaded == [("https://upload.max.test/file", b"PDF", "x.pdf")]
    assert attempts == [[{"type": "file", "payload": {"token": "ftok"}}]] * 3
    assert res.json()["message"]["attaches"][0]["name"] == "x.pdf"


def test_oversized_file_is_rejected_before_upload(monkeypatch, api, make_user):
    monkeypatch.setattr(max_service, "MAX_UPLOAD_SIZE", 2)
    monkeypatch.setattr(bot_api, "get_upload_url", lambda kind: pytest.fail("не должно грузить"))
    res = api(make_user(Module.CLIENTS)).post(
        "/api/max/messages/attachment", data={"chat_id": "555"}, files={"file": ("big.bin", b"123")},
    )
    assert res.status_code == 413


def test_empty_message_is_422(api, make_user):
    res = api(make_user(Module.CLIENTS)).post("/api/max/messages/attachment", data={"chat_id": "555", "text": " "})
    assert res.status_code == 422


def test_chat_denied_is_502_and_nothing_stored(monkeypatch, api, make_user, db):
    def denied(*a, **k):
        raise bot_api.BotApiError(403, "chat.denied", "denied")

    monkeypatch.setattr(bot_api, "send_message", denied)
    res = api(make_user(Module.CLIENTS)).post("/api/max/messages", json={"chat_id": 555, "text": "привет"})
    assert res.status_code == 502
    assert "chat.denied" in res.json()["detail"]
    assert db.query(MaxBotMessage).count() == 0


def test_no_token_is_503(monkeypatch, api, make_user):
    monkeypatch.setattr(settings, "max_bot_token", "")
    res = api(make_user(Module.CLIENTS)).post("/api/max/messages", json={"chat_id": 555, "text": "привет"})
    assert res.status_code == 503
    assert res.json()["detail"] == "MAX-бот не настроен: не задан MAX_BOT_TOKEN"


def test_attachment_url_and_media_from_stored_message(monkeypatch, api, make_user, db):
    _ingest(db, _message("mid.1", attachments=[
        {"type": "file", "payload": {"url": "https://f.max.test/s.pdf"}, "filename": "s.pdf"},
        {"type": "video", "payload": {"token": "vt", "url": "https://v.max.test/w"}},
        {"type": "audio", "payload": {"url": "https://a.max.test/a.ogg"}},
    ]))
    monkeypatch.setattr(bot_api, "get_video", lambda token: {
        "urls": {"mp4_480": "https://v.max.test/480.mp4", "mp4_1080": "https://v.max.test/1080.mp4",
                 "hls": "https://v.max.test/x.m3u8"},
    })
    worker = api(make_user(Module.CLIENTS))
    q = {"chat_id": 555, "message_id": "mid.1"}

    assert worker.get("/api/max/attachment", params={**q, "file_id": 0}).json() == {"url": "https://f.max.test/s.pdf"}
    assert worker.get("/api/max/media", params={**q, "media_id": "1"}).json() == {
        "url": "https://v.max.test/1080.mp4", "external": "https://v.max.test/w",
    }
    assert worker.get("/api/max/media", params={**q, "media_id": "2"}).json() == {
        "url": "https://a.max.test/a.ogg", "external": None,
    }
    assert worker.get("/api/max/attachment", params={**q, "file_id": 9}).status_code == 404
    # чужой чат не открывает вложения сообщения
    assert worker.get("/api/max/attachment", params={**q, "chat_id": 1, "file_id": 0}).status_code == 404


def test_warehouse_style_call_without_db_uses_own_session(monkeypatch, db):
    """warehouse.send_lead_time_question зовёт send_message(chat_id, text) без
    сессии — сервис открывает свою (SessionLocal)."""
    monkeypatch.setattr(max_service, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)
    monkeypatch.setattr(
        bot_api, "send_message",
        lambda chat_id, text, attachments=None, notify=True: _message("mid.w", text, chat_id=-501,
                                                                      chat_type="chat", sender=BOT),
    )
    res = max_service.send_message(-501, "Проставьте сроки")
    assert res["chatId"] == -501
    assert db.get(MaxBotMessage, "mid.w").is_outgoing
