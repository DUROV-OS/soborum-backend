"""POST /api/max/messages/attachment (0015) — сквозной путь service+router
с поддельной сессией MAX (websocket и HTTP-загрузка не трогают реальный MAX).
"""

import contextlib

import pytest

from app.common.module_access import Module
from app.core.config import settings
from app.max import service as max_service


class _FakeSession:
    def __init__(self):
        self.sent_attaches = None
        self.uploaded = []

    def upload_file(self, data, filename, content_type):
        self.uploaded.append((data, filename, content_type))
        return {"fileId": 42, "token": "tok"}

    def send_message(self, chat_id, text, notify=True, attaches=None):
        self.sent_attaches = attaches or []
        return {"chatId": chat_id, "message": {"id": "1", "time": 1, "text": text, "attaches": attaches or []}}

    def viewer_id(self):
        return "1"

    def contacts_by_id(self):
        return {}


@pytest.fixture
def fake_session(monkeypatch):
    fake = _FakeSession()
    monkeypatch.setattr(settings, "max_file_upload_opcode", 87)

    @contextlib.contextmanager
    def _session():
        yield fake

    monkeypatch.setattr(max_service, "session", _session)
    return fake


def test_send_message_with_file_uploads_then_sends_attach(fake_session, api, make_user):
    worker = api(make_user(Module.WAREHOUSE))
    resp = worker.post(
        "/api/max/messages/attachment",
        data={"chat_id": "0", "text": "прайс во вложении"},
        files={"file": ("price.xlsx", b"fake-xlsx-bytes", "application/vnd.ms-excel")},
    )
    assert resp.status_code == 201, resp.text
    assert fake_session.uploaded == [(b"fake-xlsx-bytes", "price.xlsx", "application/vnd.ms-excel")]
    assert fake_session.sent_attaches == [{"_type": "FILE", "fileId": 42, "token": "tok"}]


def test_send_message_with_file_and_empty_text_is_allowed(fake_session, api, make_user):
    worker = api(make_user(Module.WAREHOUSE))
    resp = worker.post(
        "/api/max/messages/attachment",
        data={"chat_id": "0"},
        files={"file": ("price.xlsx", b"data", "application/vnd.ms-excel")},
    )
    assert resp.status_code == 201, resp.text


def test_no_file_and_no_text_is_rejected(fake_session, api, make_user):
    worker = api(make_user(Module.WAREHOUSE))
    resp = worker.post("/api/max/messages/attachment", data={"chat_id": "0"})
    assert resp.status_code == 422


def test_text_only_send_still_works_without_attachment_endpoint(fake_session, api, make_user):
    """Регрессия: /messages (без файла) не сломан веткой 0015."""
    worker = api(make_user(Module.WAREHOUSE))
    resp = worker.post("/api/max/messages", json={"chat_id": 0, "text": "просто текст"})
    assert resp.status_code == 201, resp.text
    assert fake_session.sent_attaches == []
