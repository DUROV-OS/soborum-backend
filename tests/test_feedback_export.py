"""Задача 0109: read-only экспорт заявок для агента по сервисному токену."""

import io

import pytest

from app.common.module_access import Module
from app.core.config import settings

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32
TOKEN = {"X-Export-Token": "test-token"}


@pytest.fixture
def export_on(monkeypatch):
    monkeypatch.setattr(settings, "feedback_export_token", "test-token")


def _submit(client, text="Фильтр склада сбрасывается"):
    return client.post(
        "/api/feedback/requests",
        data={"text": text, "section": "warehouse"},
        files=[("screenshots", ("shot.png", io.BytesIO(PNG), "image/png"))],
    ).json()


def test_disabled_without_configured_token(api, make_user):
    client = api(make_user(Module.WAREHOUSE))

    for path in ("/export/requests", "/export/requests/1", "/export/requests/1/files/1"):
        assert client.get(f"/api/feedback{path}", headers=TOKEN).status_code == 404


def test_missing_or_wrong_token_is_401(api, make_user, export_on):
    client = api(make_user(Module.WAREHOUSE))

    assert client.get("/api/feedback/export/requests").status_code == 401
    assert client.get("/api/feedback/export/requests", headers={"X-Export-Token": "nope"}).status_code == 401


def test_export_lists_requests_without_pii(api, make_user, export_on):
    author = make_user(Module.WAREHOUSE)
    client = api(author)
    created = _submit(client)

    rows = client.get("/api/feedback/export/requests", headers=TOKEN).json()

    assert [r["id"] for r in rows] == [created["id"]]
    assert rows[0]["text"] == "Фильтр склада сбрасывается"
    assert rows[0]["author"] == {"full_name": author.full_name, "role": "worker"}
    raw = client.get("/api/feedback/export/requests", headers=TOKEN).text
    assert author.email not in raw
    assert len(rows[0]["attachments"]) == 1


def test_after_id_and_limit(api, make_user, export_on):
    client = api(make_user(Module.WAREHOUSE))
    ids = [_submit(client, f"заявка {i}")["id"] for i in range(3)]

    page = client.get(f"/api/feedback/export/requests?after_id={ids[0]}&limit=1", headers=TOKEN).json()

    assert [r["id"] for r in page] == [ids[1]]


def test_export_file_download_and_foreign_file(api, make_user, export_on):
    client = api(make_user(Module.WAREHOUSE))
    first, second = _submit(client), _submit(client)
    file_id = first["attachments"][0]["file_id"]

    ok = client.get(f"/api/feedback/export/requests/{first['id']}/files/{file_id}", headers=TOKEN)
    foreign = client.get(f"/api/feedback/export/requests/{second['id']}/files/{file_id}", headers=TOKEN)

    assert ok.status_code == 200
    assert ok.content == PNG
    assert foreign.status_code == 404


def test_export_is_read_only(api, make_user, export_on):
    client = api(make_user(Module.WAREHOUSE))

    assert client.post("/api/feedback/export/requests", headers=TOKEN).status_code == 405
    assert client.patch("/api/feedback/export/requests/1", headers=TOKEN).status_code == 405
