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
