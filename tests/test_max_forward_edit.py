"""Пересылка и правка сообщений MAX (0098): service+router с поддельной
сессией — реальный MAX не трогаем. Формы кадров взяты из живой проверки
(см. backlog 0098, «Контекст»)."""

import contextlib

import pytest

from app.common.module_access import Module
from app.max import service as max_service

VIEWER = "435168608"


class _FakeSession:
    def __init__(self, original=None):
        self.original = original
        self.edited = None
        self.forwarded = None

    def viewer_id(self):
        return VIEWER

    def contacts_by_id(self):
        return {"165105331": {"names": [{"name": "Клиент Тестовый"}]}}

    def get_message(self, chat_id, message_id):
        return self.original

    def edit_message(self, chat_id, message_id, text, attachments):
        self.edited = (chat_id, message_id, text, attachments)
        msg = dict(self.original, text=text, status="EDITED")
        return {"message": msg}

    def forward_message(self, to_chat_id, from_chat_id, message_id, notify=True):
        self.forwarded = (to_chat_id, from_chat_id, message_id)
        return {
            "chatId": to_chat_id,
            "message": {
                "sender": int(VIEWER),
                "id": "900",
                "time": 2,
                "text": "",
                "type": "USER",
                "attaches": [],
                "link": {
                    "type": "FORWARD",
                    "chatId": from_chat_id,
                    "message": {
                        "sender": 165105331,
                        "id": message_id,
                        "text": "оригинал",
                        "attaches": [{"_type": "FILE", "fileId": 5092215043, "name": "a.txt", "size": 3}],
                    },
                },
            },
        }


def _own(text="было", attaches=None, **extra):
    return {"sender": int(VIEWER), "id": "117", "time": 1, "text": text, "type": "USER",
            "attaches": attaches or [], **extra}


@pytest.fixture
def fake(monkeypatch):
    holder = {"session": _FakeSession()}

    @contextlib.contextmanager
    def _session():
        yield holder["session"]

    monkeypatch.setattr(max_service, "session", _session)
    return holder


@pytest.fixture
def worker(api, make_user):
    return api(make_user(Module.WAREHOUSE))


def test_forward_returns_original_in_forwarded(fake, worker):
    resp = worker.post(
        "/api/max/messages/forward",
        json={"from_chat_id": 271018963, "message_id": "117326069508097035", "to_chat_id": 0},
    )
    assert resp.status_code == 201, resp.text
    assert fake["session"].forwarded == (0, 271018963, "117326069508097035")
    fwd = resp.json()["message"]["forwarded"]
    assert fwd["senderName"] == "Клиент Тестовый"
    assert fwd["chatId"] == 271018963
    assert fwd["text"] == "оригинал"
    assert fwd["attaches"][0]["fileId"] == "5092215043"


def test_plain_message_has_no_forwarded():
    assert max_service._fmt_msg(_own(), VIEWER)["forwarded"] is None


def test_edit_text_message(fake, worker):
    fake["session"].original = _own()
    resp = worker.patch("/api/max/messages", json={"chat_id": 0, "message_id": "117", "text": " стало "})
    assert resp.status_code == 200, resp.text
    assert fake["session"].edited == (0, "117", "стало", [])
    assert resp.json()["message"]["status"] == "EDITED"


def test_edit_keeps_file_attachment(fake, worker):
    """MSG_EDIT с attachments=[] удаляет файл — передаём его заново."""
    fake["session"].original = _own(attaches=[{"_type": "FILE", "fileId": 5166137841, "name": "a.pdf"}])
    resp = worker.patch("/api/max/messages", json={"chat_id": 0, "message_id": "117", "text": "новый"})
    assert resp.status_code == 200, resp.text
    assert fake["session"].edited[3] == [{"_type": "FILE", "fileId": 5166137841}]


@pytest.mark.parametrize(
    "original, code",
    [
        (None, 404),
        ({"sender": 165105331, "id": "117", "text": "чужое", "attaches": []}, 403),
        (_own(text="", link={"type": "FORWARD", "chatId": 1, "message": {"text": "x"}}), 409),
        (_own(attaches=[{"_type": "PHOTO", "photoId": 1, "photoToken": "t"}]), 409),
    ],
)
def test_edit_refusals_do_not_touch_max(fake, worker, original, code):
    fake["session"].original = original
    resp = worker.patch("/api/max/messages", json={"chat_id": 0, "message_id": "117", "text": "новый"})
    assert resp.status_code == code, resp.text
    assert fake["session"].edited is None


def test_edit_empty_text_without_file_is_rejected(fake, worker):
    fake["session"].original = _own()
    resp = worker.patch("/api/max/messages", json={"chat_id": 0, "message_id": "117", "text": "  "})
    assert resp.status_code == 422
    assert fake["session"].edited is None


def test_message_id_must_be_numeric(fake, worker):
    resp = worker.patch("/api/max/messages", json={"chat_id": 0, "message_id": "abc", "text": "x"})
    assert resp.status_code == 422
