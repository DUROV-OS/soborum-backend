"""Загрузка вложений в MAX (0015): MaxSession.upload_file и интеграция в
send_message. Websocket и HTTP-загрузка подменены — реальный MAX не трогаем.
"""

import pytest
from fastapi import HTTPException

from app.core.config import settings
from app.max.client import MAX_UPLOAD_SIZE, MaxSession, UploadError


class _FakeUploadSession(MaxSession):
    """Подменяет только _send/_wait (websocket-кадры) — upload_file вызывает
    их напрямую, остальной MaxSession не трогаем."""

    def __init__(self, prepare_reply):
        self.prepare_reply = prepare_reply
        self.sent = []

    def _send(self, opcode, payload):
        self.sent.append((opcode, payload))
        return 1

    def _wait(self, opcode, seq=None, tries=80):
        return self.prepare_reply


def test_upload_file_requires_configured_opcode(monkeypatch):
    monkeypatch.setattr(settings, "max_file_upload_opcode", None)
    s = _FakeUploadSession(prepare_reply={})
    with pytest.raises(HTTPException) as exc:
        s.upload_file(b"data", "a.txt", "text/plain")
    assert exc.value.status_code == 503
    assert "не настроена" in exc.value.detail


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
        prepare_reply={"payload": {"info": [{"fileId": 42, "url": "https://upload.example/x", "token": "tok"}]}}
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
