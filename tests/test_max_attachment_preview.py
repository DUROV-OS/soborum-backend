"""GET /api/max/attachment/preview (0031): прокси вложения с корректным
content-type и без обращения к настоящему MAX (get_attachment_url подменена).
"""

import base64

import httpx
import pytest
from fastapi.encoders import jsonable_encoder

from app.common.module_access import Module
from app.max import service as max_service


def test_binary_poster_is_json_safe_in_chat_list_and_history():
    poster = b"RIFF\xf8\x00\x00\x00WEBPVP8 "
    thumbnail = "https://example.test/preview"
    message = {"id": 12, "attaches": [{"_type": "VIDEO", "previewData": poster, "thumbnail": thumbnail}]}
    chat = max_service._fmt_chat({"id": 10, "lastMessage": message}, {}, {})
    preview = "data:image/webp;base64," + base64.b64encode(poster).decode("ascii")
    for item in (chat["lastMessage"], max_service._fmt_msg(message)):
        attach = jsonable_encoder(item)["attaches"][0]
        assert attach["previewData"] == preview and attach["thumbnail"] == thumbnail


def test_unreadable_binary_preview_keeps_attachment_and_fallback():
    attach = max_service._fmt_attach({"_type": "VIDEO", "videoId": 14, "previewData": b"\xff\xfe",
                                     "thumbnail": "https://example.test/poster"})
    assert attach == {"type": "VIDEO", "videoId": "14", "thumbnail": "https://example.test/poster"}


class _FakeStreamResponse:
    def __init__(self, chunks: list[bytes], headers: dict | None = None):
        self._chunks = chunks
        self.headers = headers or {}

    def raise_for_status(self):
        pass

    def iter_bytes(self):
        yield from self._chunks

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_preview_supported_extension_returns_bytes_with_content_type(monkeypatch, api, make_user):
    monkeypatch.setattr(max_service, "get_attachment_url", lambda *a, **k: "https://fd.oneme.ru/getfile?x")
    monkeypatch.setattr(
        httpx, "stream", lambda *a, **k: _FakeStreamResponse([b"%PDF-1.4 fake"])
    )
    worker = api(make_user(Module.WAREHOUSE))

    resp = worker.get(
        "/api/max/attachment/preview",
        params={"chat_id": -1, "message_id": "1", "file_id": 1, "filename": "смета.pdf"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/pdf")
    assert resp.content == b"%PDF-1.4 fake"


def test_preview_rejects_unsupported_extension_without_calling_max(monkeypatch, api, make_user):
    called = []
    monkeypatch.setattr(max_service, "get_attachment_url", lambda *a, **k: called.append(1))
    worker = api(make_user(Module.WAREHOUSE))

    resp = worker.get(
        "/api/max/attachment/preview",
        params={"chat_id": -1, "message_id": "1", "file_id": 1, "filename": "договор.docx"},
    )
    assert resp.status_code == 422
    assert called == []


def test_preview_rejects_file_over_size_limit_via_content_length(monkeypatch, api, make_user):
    monkeypatch.setattr(max_service, "get_attachment_url", lambda *a, **k: "https://fd.oneme.ru/getfile?x")
    monkeypatch.setattr(
        httpx,
        "stream",
        lambda *a, **k: _FakeStreamResponse([b"x"], headers={"content-length": str(20 * 1024 * 1024)}),
    )
    worker = api(make_user(Module.WAREHOUSE))

    resp = worker.get(
        "/api/max/attachment/preview",
        params={"chat_id": -1, "message_id": "1", "file_id": 1, "filename": "big.png"},
    )
    assert resp.status_code == 413


def test_preview_rejects_file_over_size_limit_when_length_header_missing(monkeypatch, api, make_user):
    monkeypatch.setattr(max_service, "get_attachment_url", lambda *a, **k: "https://fd.oneme.ru/getfile?x")
    big_chunk = b"x" * (max_service.PREVIEW_MAX_SIZE + 1)
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: _FakeStreamResponse([big_chunk]))
    worker = api(make_user(Module.WAREHOUSE))

    resp = worker.get(
        "/api/max/attachment/preview",
        params={"chat_id": -1, "message_id": "1", "file_id": 1, "filename": "big.txt"},
    )
    assert resp.status_code == 413
