"""Загрузка вложений в MAX (0015): MaxSession.upload_file и интеграция в
send_message. Websocket и HTTP-загрузка подменены — реальный MAX не трогаем.
"""

import pytest

from app.core.config import settings
from app.max.client import MAX_UPLOAD_SIZE, NOTIF_ATTACH, MaxSession, UploadError


class _FakeUploadSession(MaxSession):
    """Подменяет только _send/_wait (websocket-кадры) — upload_file вызывает
    их напрямую, остальной MaxSession не трогаем."""

    def __init__(self, prepare_reply, notifications=None):
        self.prepare_reply = prepare_reply
        self.notifications = list(notifications or [])
        self.sent = []

    def _send(self, opcode, payload):
        self.sent.append((opcode, payload))
        return 1

    def _wait(self, opcode, seq=None, tries=80):
        return self.prepare_reply

    def _recv(self):
        if not self.notifications:
            raise TimeoutError("кадров больше нет")
        return self.notifications.pop(0)


def test_upload_opcode_defaults_to_file_upload():
    assert settings.max_file_upload_opcode == 87


def test_upload_file_rejects_oversized_payload(monkeypatch):
    monkeypatch.setattr(settings, "max_file_upload_opcode", 87)
    s = _FakeUploadSession(prepare_reply={})
    with pytest.raises(UploadError):
        s.upload_file(b"x" * (MAX_UPLOAD_SIZE + 1), "big.bin", "application/octet-stream")


def test_upload_file_rejects_server_refusal(monkeypatch):
    monkeypatch.setattr(settings, "max_file_upload_opcode", 87)
    s = _FakeUploadSession(prepare_reply={"cmd": 3, "payload": {"message": "nope"}})
    with pytest.raises(UploadError):
        s.upload_file(b"data", "a.txt", "text/plain")


def test_upload_file_posts_bytes_with_content_range(monkeypatch):
    monkeypatch.setattr(settings, "max_file_upload_opcode", 87)
    s = _FakeUploadSession(
        prepare_reply={"payload": {"info": [{"fileId": 42, "url": "https://upload.example/x", "token": "tok"}]}},
        notifications=[{"opcode": NOTIF_ATTACH, "payload": {"fileId": 42}}],
    )

    captured = {}

    class _FakeResponse:
        def raise_for_status(self):
            pass

    def fake_post(url, content, headers, timeout):
        captured["url"] = url
        captured["content"] = content
        captured["headers"] = headers
        return _FakeResponse()

    monkeypatch.setattr("app.max.client.httpx.post", fake_post)

    result = s.upload_file(b"hello world", "note.txt", "text/plain")

    assert result == {"fileId": 42, "token": "tok"}
    assert captured["url"] == "https://upload.example/x"
    assert captured["content"] == b"hello world"
    assert captured["headers"]["Content-Type"] == "text/plain"
    assert captured["headers"]["Content-Range"] == "0-10/11"
    assert "note.txt" in captured["headers"]["Content-Disposition"]


def _upload_session(monkeypatch, notifications):
    class _Ok:
        def raise_for_status(self):
            pass

    monkeypatch.setattr("app.max.client.httpx.post", lambda *a, **kw: _Ok())
    return _FakeUploadSession(
        prepare_reply={"payload": {"info": [{"fileId": 42, "url": "https://upload.example/x", "token": "tok"}]}},
        notifications=notifications,
    )


def test_upload_file_waits_for_own_attach_notification(monkeypatch):
    """Чужие кадры и NOTIF_ATTACH другого файла пропускаются, ждём свой fileId."""
    s = _upload_session(monkeypatch, [
        {"opcode": 128, "payload": {"chatId": 1}},
        {"opcode": NOTIF_ATTACH, "payload": {"fileId": 7}},
        {"opcode": NOTIF_ATTACH, "payload": {"fileId": 42}},
    ])
    assert s.upload_file(b"data", "a.txt", "text/plain")["fileId"] == 42
    assert s.notifications == []


def test_upload_file_fails_without_attach_notification(monkeypatch):
    """MAX не подтвердил обработку → UploadError, сообщение не отправляется."""
    s = _upload_session(monkeypatch, [{"opcode": NOTIF_ATTACH, "payload": {"fileId": 7}}])
    with pytest.raises(UploadError, match="не подтвердил"):
        s.upload_file(b"data", "a.txt", "text/plain")
