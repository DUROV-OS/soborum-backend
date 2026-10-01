"""Задача 0010: канал записи в базу знаний — фича-флаг, идемпотентность,
деградации. RemoteMcp.call_tool замокан на уровне JSON-RPC - никакой реальный
MCP-сервер в этих тестах не участвует (его нет в песочнице, см. «Разведка» в
backlog/PROCESS/0010-knowledge-base-write-connector.md)."""

import httpx
import pytest
from fastapi import HTTPException

from app.ai import mcp_auth, mcp_write
from app.common.module_access import Module
from app.core.config import settings
from app.core.mcp_remote import McpError, RemoteMcp


@pytest.fixture
def mcp_configured(monkeypatch):
    """Коннектор БЗ «настроен» (mcp_configured=True), запись пока не включена -
    как и должно быть, отдельно от общего mcp_configured."""
    monkeypatch.setattr(settings, "mcp_server_url", "https://kb.example/mcp")
    monkeypatch.setattr(settings, "mcp_oauth_client_id", "client-id")
    monkeypatch.setattr(settings, "mcp_oauth_client_secret", "client-secret")
    monkeypatch.setattr(settings, "mcp_notes_folder", "02_Business/03_Meetings")


@pytest.fixture
def mcp_write_enabled(mcp_configured, monkeypatch):
    monkeypatch.setattr(settings, "mcp_write_enabled", True)


@pytest.fixture
def fake_token(monkeypatch, db):
    """get_access_token не должен реально ходить в сеть в этих тестах."""
    monkeypatch.setattr(mcp_auth, "get_access_token", lambda db, force=False: "fake-token")


# --- фича-флаг / деградация --------------------------------------------------


def test_create_note_requires_configured_connector(db):
    with pytest.raises(HTTPException) as exc:
        mcp_write.create_note(db, source="test:1", body="текст", title="Заметка")
    assert exc.value.status_code == 409
    assert "не подключена" in exc.value.detail


def test_create_note_requires_write_enabled_flag(mcp_configured, db):
    # коннектор настроен, но MCP_WRITE_ENABLED всё ещё по умолчанию False
    assert settings.mcp_configured is True
    assert settings.mcp_write_enabled is False
    with pytest.raises(HTTPException) as exc:
        mcp_write.create_note(db, source="test:1", body="текст", title="Заметка")
    assert exc.value.status_code == 409
    assert "отключена" in exc.value.detail


# --- happy path + идемпотентность -------------------------------------------


def test_create_note_calls_create_note_tool_with_frontmatter(mcp_write_enabled, fake_token, db, monkeypatch):
    calls = []

    def fake_call_tool(self, name, arguments=None, timeout=30.0):
        calls.append((name, arguments))
        return {"path": arguments["path"]}

    monkeypatch.setattr(RemoteMcp, "call_tool", fake_call_tool)

    path = mcp_write.create_note(db, source="manual-test:1", body="Тело заметки", title="Моя заметка")

    assert path == "02_Business/03_Meetings/manual-test-1.md"
    assert len(calls) == 1
    name, arguments = calls[0]
    assert name == "create_note"
    assert arguments["path"] == path
    assert "title: Моя заметка" in arguments["content"]
    assert "kind: record" in arguments["content"]
    assert "status: active" in arguments["content"]
    assert "source: manual-test:1" in arguments["content"]
    assert "Тело заметки" in arguments["content"]


def test_resend_same_source_falls_back_to_edit_note_not_a_second_create(
    mcp_write_enabled, fake_token, db, monkeypatch
):
    calls = []

    def fake_call_tool(self, name, arguments=None, timeout=30.0):
        calls.append(name)
        if name == "create_note" and calls.count("create_note") == 2:
            # второй create_note на тот же путь - «уже существует»
            raise McpError("path already exists")
        return {"ok": True}

    monkeypatch.setattr(RemoteMcp, "call_tool", fake_call_tool)

    first_path = mcp_write.create_note(db, source="meeting:42", body="v1", title="Совещание")
    second_path = mcp_write.create_note(db, source="meeting:42", body="v2 (обновлено)", title="Совещание")

    assert first_path == second_path
    # 1-й send: только create_note. 2-й send: create_note (падает) + edit_note (успех).
    assert calls == ["create_note", "create_note", "edit_note"]


def test_raw_body_used_as_is_no_double_frontmatter(mcp_write_enabled, fake_token, db, monkeypatch):
    """meeting_notes.build_document уже добавляет собственный frontmatter -
    raw=True не должен оборачивать его повторно."""
    calls = []

    def fake_call_tool(self, name, arguments=None, timeout=30.0):
        calls.append((name, arguments))
        return {"ok": True}

    monkeypatch.setattr(RemoteMcp, "call_tool", fake_call_tool)

    document = "---\ntitle: Совещание\nkind: record\n---\n\n# Совещание\n\nтекст"
    mcp_write.create_note(db, source="meeting:7", body=document, raw=True)

    assert calls[0][1]["content"] == document


def test_create_note_raw_without_title_does_not_require_it(mcp_write_enabled, fake_token, db, monkeypatch):
    monkeypatch.setattr(RemoteMcp, "call_tool", lambda self, name, arguments=None, timeout=30.0: {"ok": True})
    # не должно бросать ValueError о title, когда raw=True
    mcp_write.create_note(db, source="meeting:8", body="документ целиком", raw=True)


def test_create_note_non_raw_without_title_raises(mcp_write_enabled, fake_token, db):
    with pytest.raises(ValueError):
        mcp_write.create_note(db, source="x", body="текст")


# --- отказы -------------------------------------------------------------


def test_both_create_and_edit_fail_surfaces_clean_502(mcp_write_enabled, fake_token, db, monkeypatch):
    def fake_call_tool(self, name, arguments=None, timeout=30.0):
        raise McpError(f"{name} недоступен")

    monkeypatch.setattr(RemoteMcp, "call_tool", fake_call_tool)

    with pytest.raises(HTTPException) as exc:
        mcp_write.create_note(db, source="x", body="текст", title="Т")
    assert exc.value.status_code == 502


def test_unreachable_server_is_a_clean_error_no_retry_storm(mcp_write_enabled, fake_token, db, monkeypatch):
    attempts = []

    def fake_call_tool(self, name, arguments=None, timeout=30.0):
        attempts.append(name)
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(RemoteMcp, "call_tool", fake_call_tool)

    with pytest.raises(HTTPException) as exc:
        mcp_write.create_note(db, source="x", body="текст", title="Т")
    assert exc.value.status_code == 502
    # ровно одна попытка create_note + одна edit_note (fallback), без ретрай-шторма
    assert attempts == ["create_note", "edit_note"]


def test_append_note_degrades_cleanly_when_disabled(db):
    with pytest.raises(HTTPException) as exc:
        mcp_write.append_note(db, path="x.md", text="ещё текст")
    assert exc.value.status_code == 409


# --- токен: разделяемый с read-коннектором, форс-регрант при 401/403 -------


def test_client_uses_mcp_auth_get_access_token(mcp_write_enabled, db, monkeypatch):
    seen = []

    def fake_get_access_token(db_arg, force=False):
        seen.append(force)
        return "tok"

    monkeypatch.setattr(mcp_auth, "get_access_token", fake_get_access_token)

    client = mcp_write._client(db)
    assert client._access_token() == "tok"
    assert seen == [False]
    client._forget_auth()
    assert client._access_token() == "tok"
    assert seen == [False, True]  # форс-регрант после 401/403 (см. RemoteMcp._rpc)


# --- модель доступа: write-инструменты не в общем allowed_tools -------------


def test_write_tools_never_enter_assistant_allowed_tools(mcp_write_enabled, fake_token, db, make_user):
    from app.ai import engine
    from app.ai.models import Chat, ChatDomain, ChatMode

    admin = make_user(admin=True)
    chat = Chat(owner_id=admin.id, domain=ChatDomain.GENERAL, mode=ChatMode.NO_ACTIONS)
    db.add(chat)
    db.commit()
    db.refresh(chat)

    _kwargs, mcp_servers = engine._build_request(db, "system", [], [], admin)
    assert mcp_servers is not None
    allowed = mcp_servers[0]["tool_configuration"]["allowed_tools"]
    for write_tool in engine.MCP_WRITE_TOOLS:
        assert write_tool not in allowed
    assert allowed == engine.MCP_READ_ONLY_TOOLS


def test_regular_employee_cannot_reach_admin_notes_endpoint(api, make_user, mcp_write_enabled):
    worker = make_user(Module.AI)
    resp = api(worker).post(
        "/api/ai/mcp/notes", json={"title": "T", "body": "тело", "source": "manual-test:2"}
    )
    assert resp.status_code == 403


def test_admin_notes_endpoint_creates_note(api, make_user, mcp_write_enabled, monkeypatch):
    monkeypatch.setattr(RemoteMcp, "call_tool", lambda self, name, arguments=None, timeout=30.0: {"ok": True})
    admin = make_user(admin=True)
    resp = api(admin).post(
        "/api/ai/mcp/notes", json={"title": "T", "body": "тело", "source": "manual-test:3"}
    )
    assert resp.status_code == 201
    assert resp.json()["path"] == "02_Business/03_Meetings/manual-test-3.md"


def test_admin_notes_endpoint_409_when_write_disabled(api, make_user):
    admin = make_user(admin=True)
    resp = api(admin).post(
        "/api/ai/mcp/notes", json={"title": "T", "body": "тело", "source": "manual-test:4"}
    )
    assert resp.status_code == 409
