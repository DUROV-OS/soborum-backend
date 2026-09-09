from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.ai import engine
from app.ai.models import AiCacheEntry, Chat, ChatDomain, ChatMode, Message, PendingAction, PendingActionStatus
from app.ai.tools import TOOLS
from app.common.module_access import Module
from app.cycle.models import Cycle
from app.production.models import Production


def tool_response(name, inputs):
    content = {"type": "tool_use", "id": "call-1", "name": name, "input": inputs}
    return SimpleNamespace(stop_reason="tool_use", content=[SimpleNamespace(model_dump=lambda **_: content)])


def final_response():
    return SimpleNamespace(stop_reason="end_turn", content=[])


def make_chat(db, user, mode=ChatMode.REQUIRE_APPROVAL, domain=ChatDomain.GENERAL):
    chat = Chat(owner_id=user.id, domain=domain, mode=mode)
    db.add(chat)
    db.commit()
    return chat


def make_pending(db, user):
    chat = make_chat(db, user)
    message = Message(chat_id=chat.id, role="assistant", content=[{
        "type": "tool_use", "id": "call-1", "name": "add_client_note", "input": {"client_id": 1, "text": "Тест"}
    }], tool_resolutions={"call-1": {"status": "pending"}})
    db.add(message)
    db.flush()
    action = PendingAction(chat_id=chat.id, message_id=message.id, tool_use_id="call-1",
                           tool_name="add_client_note", tool_input={"client_id": 1, "text": "Тест"})
    db.add(action)
    db.commit()
    return action


def test_today_works_without_ai_access_or_provider(api, make_user):
    response = api(make_user(Module.PRODUCTION)).get("/api/dashboard/today")
    assert response.status_code == 200
    assert {w["section"] for w in response.json()["widgets"]} == {"production"}


def test_dashboard_does_not_serve_data_after_access_revocation(db, api, make_user):
    user = make_user(Module.AI, Module.PRODUCTION)
    cached = AiCacheEntry(key=f"today_dashboard:{user.id}", generated_at=datetime.now(timezone.utc), payload={
        "generated_at": datetime.now(timezone.utc).isoformat(), "summary": "Private sales data",
        "widgets": [{"section": "clients", "title": "Secret", "value": "9000000", "tone": "neutral"}],
    })
    db.add(cached)
    db.commit()
    response = api(user).get("/api/dashboard/today")
    assert response.status_code == 200
    assert "Private sales data" not in response.text
    assert all(w["section"] == "production" for w in response.json()["widgets"])


def test_production_list_does_not_require_cycle_permission(db, api, make_user):
    cycle = Cycle()
    db.add(cycle)
    db.flush()
    production = Production(cycle_id=cycle.id)
    db.add(production)
    db.commit()
    response = api(make_user(Module.PRODUCTION)).get("/api/production/")
    assert response.status_code == 200
    assert response.json()[0]["id"] == production.id
    assert "client" not in response.json()[0]


@pytest.mark.parametrize("mode,modules,name,inputs", [
    (ChatMode.NO_ACTIONS, [Module.AI, Module.CLIENTS], "add_client_note", {"client_id": 1, "text": "Тест"}),
    (ChatMode.REQUIRE_APPROVAL, [Module.AI], "list_clients", {}),
])
def test_unadvertised_tool_cannot_bypass_server_policy(db, make_user, monkeypatch, mode, modules, name, inputs):
    user = make_user(*modules)
    chat = make_chat(db, user, mode)
    handler = Mock(return_value={"private": "data"})
    monkeypatch.setattr(TOOLS[name], "handler", handler)
    monkeypatch.setattr(engine, "_call_claude", Mock(side_effect=[tool_response(name, inputs), final_response()]))
    engine._advance(db, chat, user)
    handler.assert_not_called()
    db.refresh(chat)
    resolutions = [m.tool_resolutions for m in chat.messages if m.tool_resolutions]
    assert resolutions[0]["call-1"]["is_error"] is True


def test_legacy_auto_mode_still_requires_approval_for_mutation(db, make_user, monkeypatch):
    user = make_user(Module.AI, Module.CLIENTS)
    chat = make_chat(db, user, ChatMode.AUTO_APPROVE)
    handler = Mock(return_value={"ok": True})
    monkeypatch.setattr(TOOLS["add_client_note"], "handler", handler)
    monkeypatch.setattr(engine, "_call_claude", Mock(side_effect=[
        tool_response("add_client_note", {"client_id": 1, "text": "Тест"}), final_response()]))
    result = engine._advance(db, chat, user)
    handler.assert_not_called()
    assert result.status == "pending_approval"


def test_approval_rechecks_revoked_module_permission(db, make_user, monkeypatch):
    user = make_user(Module.AI, Module.CLIENTS)
    action = make_pending(db, user)
    user.module_access = [grant for grant in user.module_access if grant.module != Module.CLIENTS]
    db.commit()
    handler = Mock(return_value={"ok": True})
    monkeypatch.setattr(TOOLS["add_client_note"], "handler", handler)
    monkeypatch.setattr(engine, "_advance", Mock(return_value=engine.TurnResult(status="completed")))
    with pytest.raises(HTTPException) as error:
        engine.resolve_pending_action(db, action, True, user)
    assert error.value.status_code == 403
    handler.assert_not_called()
    assert action.status == PendingActionStatus.PENDING


def test_web_tools_offered_when_enabled(monkeypatch):
    monkeypatch.setattr(engine.settings, "web_tools_enabled", True)
    monkeypatch.setattr(engine.settings, "web_search_max_uses", 3)
    names = {t["name"] for t in engine._server_tools()}
    assert names == {"web_search", "web_fetch"}
    assert next(t for t in engine._server_tools() if t["name"] == "web_search")["max_uses"] == 3


def test_web_tools_removed_by_flag(monkeypatch):
    monkeypatch.setattr(engine.settings, "web_tools_enabled", False)
    assert engine._server_tools() == []


def test_pause_turn_resumes_without_ending_the_turn(db, make_user, monkeypatch):
    user = make_user(Module.AI)
    chat = make_chat(db, user)
    paused = SimpleNamespace(stop_reason="pause_turn",
                             content=[SimpleNamespace(model_dump=lambda **_: {"type": "server_tool_use", "id": "s1"})])
    done = SimpleNamespace(stop_reason="end_turn",
                           content=[SimpleNamespace(model_dump=lambda **_: {"type": "text", "text": "40 м², 3 млн ₽"})])
    call = Mock(side_effect=[paused, done])
    monkeypatch.setattr(engine, "_call_claude", call)
    db.add(Message(chat_id=chat.id, role="user", content=[{"type": "text", "text": "площадь DH-64?"}]))
    db.commit()
    db.refresh(chat)
    result = engine._advance(db, chat, user)
    assert call.call_count == 2
    assert result.status == "completed"
    assert result.reply == "40 м², 3 млн ₽"


def test_stale_second_approval_cannot_execute_twice(db, make_user, monkeypatch):
    user = make_user(Module.AI, Module.CLIENTS)
    action = make_pending(db, user)
    handler = Mock(return_value={"ok": True})
    monkeypatch.setattr(TOOLS["add_client_note"], "handler", handler)
    monkeypatch.setattr(engine, "_advance", Mock(return_value=engine.TurnResult(status="completed")))
    with Session(db.bind, expire_on_commit=False) as second:
        stale_action = second.get(PendingAction, action.id)
        second_user = second.get(type(user), user.id)
        engine.resolve_pending_action(db, action, True, user)
        with pytest.raises(HTTPException) as error:
            engine.resolve_pending_action(second, stale_action, True, second_user)
        assert error.value.status_code == 409
    assert handler.call_count == 1


# --- Streaming turn (SSE) --------------------------------------------------------

class _FakeStream:
    """Stand-in for client.messages.stream(...)'s context manager."""

    def __init__(self, events, final):
        self._events, self._final = events, final

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(self._events)

    def get_final_message(self):
        return self._final


def _ev(type_, **kw):
    return SimpleNamespace(type=type_, **kw)


def _text_events(*chunks):
    yield _ev("content_block_start", content_block=SimpleNamespace(type="text"))
    for chunk in chunks:
        yield _ev("content_block_delta", delta=SimpleNamespace(type="text_delta", text=chunk))
    yield _ev("content_block_stop")


def _final(stop_reason, blocks):
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(model_dump=lambda _b=b, **_: _b) for b in blocks],
    )


def test_stream_yields_tokens_then_done_and_persists(db, make_user, monkeypatch):
    user = make_user(Module.AI)
    chat = make_chat(db, user)
    stream = _FakeStream(list(_text_events("40 ", "м²")), _final("end_turn", [{"type": "text", "text": "40 м²"}]))
    monkeypatch.setattr(engine, "_stream_claude", Mock(return_value=stream))

    events = list(engine._advance_stream(db, chat, user))

    assert [e["type"] for e in events] == ["block_start", "text", "text", "block_end", "done"]
    assert "".join(e["text"] for e in events if e["type"] == "text") == "40 м²"
    db.refresh(chat)
    assert [m.role for m in chat.messages] == ["assistant"]
    assert chat.messages[0].content == [{"type": "text", "text": "40 м²"}]


def test_stream_reports_web_search_as_status(db, make_user, monkeypatch):
    user = make_user(Module.AI)
    chat = make_chat(db, user)
    events = [_ev("content_block_start", content_block=SimpleNamespace(type="server_tool_use", name="web_search"))]
    events += list(_text_events("готово"))
    stream = _FakeStream(events, _final("end_turn", [{"type": "text", "text": "готово"}]))
    monkeypatch.setattr(engine, "_stream_claude", Mock(return_value=stream))

    out = list(engine._advance_stream(db, chat, user))

    assert {"type": "status", "text": "Ищу в интернете…"} in out
    assert out[-1]["type"] == "done"


def test_stream_stops_on_pending_approval(db, make_user, monkeypatch):
    user = make_user(Module.AI, Module.CLIENTS)
    chat = make_chat(db, user, ChatMode.REQUIRE_APPROVAL)
    db.add(Message(chat_id=chat.id, role="user", content=[{"type": "text", "text": "добавь заметку клиенту 1"}]))
    db.commit()
    tool_call = {"type": "tool_use", "id": "call-1", "name": "add_client_note",
                 "input": {"client_id": 1, "text": "Тест"}}
    calls = Mock(side_effect=[_FakeStream([], _final("tool_use", [tool_call]))])
    monkeypatch.setattr(engine, "_stream_claude", calls)
    handler = Mock(return_value={"ok": True})
    monkeypatch.setattr(TOOLS["add_client_note"], "handler", handler)

    out = list(engine._advance_stream(db, chat, user))

    handler.assert_not_called()
    assert calls.call_count == 1
    pending = next(e for e in out if e["type"] == "pending_approval")
    assert pending["pending_actions"][0]["tool_name"] == "add_client_note"
    assert pending["pending_actions"][0]["status"] == "pending"
    assert out[-1]["type"] == "pending_approval"


def test_stream_resumes_after_pause_turn(db, make_user, monkeypatch):
    user = make_user(Module.AI)
    chat = make_chat(db, user)
    paused = _FakeStream([], _final("pause_turn", [{"type": "server_tool_use", "id": "s1"}]))
    done = _FakeStream(list(_text_events("30 м²")), _final("end_turn", [{"type": "text", "text": "30 м²"}]))
    calls = Mock(side_effect=[paused, done])
    monkeypatch.setattr(engine, "_stream_claude", calls)

    out = list(engine._advance_stream(db, chat, user))

    assert calls.call_count == 2
    assert out[-1]["type"] == "done"
    assert "".join(e["text"] for e in out if e["type"] == "text") == "30 м²"
