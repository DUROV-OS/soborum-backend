from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from app.ai import engine, service
from app.ai.models import ChatDomain, ChatMode, PendingActionStatus
from app.ai.router import _to_pending_out
from app.ai.tools import TOOLS
from app.common.module_access import Module
from app.core.config import settings
from test_pilot_regressions import make_chat, make_pending, tool_response, final_response


def test_production_data_requires_production_access(api, make_user):
    assert api(make_user(Module.AI)).get("/api/production/").status_code == 403


def test_tool_from_another_domain_is_blocked(db, make_user, monkeypatch):
    user = make_user(Module.AI, Module.CLIENTS, Module.PRODUCTION)
    chat = make_chat(db, user, domain=ChatDomain.PRODUCTION)
    handler = Mock(return_value={"private": "data"})
    monkeypatch.setattr(TOOLS["list_clients"], "handler", handler)
    monkeypatch.setattr(engine, "_call_claude", Mock(side_effect=[tool_response("list_clients", {}), final_response()]))
    engine._advance(db, chat, user)
    handler.assert_not_called()


@pytest.mark.parametrize("inputs", [{"client_id": True, "text": "Test"}, {"client_id": 1, "text": "Test", "user": 1}])
def test_invalid_model_arguments_never_create_pending_actions(db, make_user, monkeypatch, inputs):
    user = make_user(Module.AI, Module.CLIENTS)
    chat = make_chat(db, user)
    monkeypatch.setattr(engine, "_call_claude", Mock(side_effect=[tool_response("add_client_note", inputs), final_response()]))
    result = engine._advance(db, chat, user)
    assert result.status == "completed"
    assert service.list_own_pending_actions(db, user) == []


def test_failed_mutation_rolls_back_and_records_failure(db, make_user, monkeypatch):
    user = make_user(Module.AI, Module.CLIENTS)
    original_name = user.full_name
    action = make_pending(db, user)
    def fail(db, user, **kwargs):
        user.full_name = "Should not persist"
        db.flush()
        raise HTTPException(409, "Business precondition changed")
    monkeypatch.setattr(TOOLS["add_client_note"], "handler", fail)
    monkeypatch.setattr(engine, "_advance", Mock(return_value=engine.TurnResult(status="completed")))
    engine.resolve_pending_action(db, action, True, user)
    db.refresh(user)
    assert user.full_name == original_name
    assert action.status == PendingActionStatus.APPROVED
    assert _to_pending_out(action).execution_status == "failed"


def test_provider_outage_after_success_does_not_report_action_failure(db, make_user, monkeypatch):
    user = make_user(Module.AI, Module.CLIENTS)
    action = make_pending(db, user)
    handler = Mock(return_value={"note_id": 1})
    monkeypatch.setattr(TOOLS["add_client_note"], "handler", handler)
    monkeypatch.setattr(engine, "_advance", Mock(side_effect=HTTPException(503, "Provider offline")))
    result = engine.resolve_pending_action(db, action, True, user)
    assert result.status == "completed"
    assert _to_pending_out(action).execution_status == "succeeded"
    assert handler.call_count == 1


def test_decision_history_cannot_be_deleted_with_chat(db, make_user):
    user = make_user(Module.AI, Module.CLIENTS)
    action = make_pending(db, user)
    with pytest.raises(HTTPException) as error:
        service.delete_chat(db, action.chat)
    assert error.value.status_code == 409


def test_new_turn_is_blocked_while_decision_is_pending(db, make_user):
    user = make_user(Module.AI, Module.CLIENTS)
    action = make_pending(db, user)
    with pytest.raises(HTTPException) as error:
        engine.run_turn(db, action.chat, user, "Продолжай")
    assert error.value.status_code == 409


@pytest.mark.parametrize("admin", [True, False])
def test_shared_knowledge_connector_never_gets_write_permissions(db, make_user, monkeypatch, admin):
    user = make_user(Module.AI, admin=admin)
    monkeypatch.setattr(settings, "mcp_server_url", "https://knowledge.invalid")
    monkeypatch.setattr(settings, "mcp_oauth_client_id", "fixture")
    monkeypatch.setattr(settings, "mcp_oauth_client_secret", "fixture")
    monkeypatch.setattr(engine.mcp_auth, "get_access_token", Mock(return_value="fixture"))
    client = Mock()
    monkeypatch.setattr(engine, "_get_client", Mock(return_value=client))
    engine._call_claude(db, "system", [], [], ChatMode.AUTO_APPROVE, user)
    if admin:
        connector = client.beta.messages.create.call_args.kwargs["mcp_servers"][0]
        assert connector["tool_configuration"]["allowed_tools"] == engine.MCP_READ_ONLY_TOOLS
    else:
        client.beta.messages.create.assert_not_called()
        client.messages.create.assert_called_once()


def test_truncated_tool_call_is_not_replayed_or_executed(db, make_user, monkeypatch):
    user = make_user(Module.AI, Module.CLIENTS)
    chat = make_chat(db, user)
    response = tool_response("add_client_note", {"client_id": 1, "text": "incomplete"})
    response.stop_reason = "max_tokens"
    handler = Mock()
    monkeypatch.setattr(TOOLS["add_client_note"], "handler", handler)
    monkeypatch.setattr(engine, "_call_claude", Mock(return_value=response))
    result = engine._advance(db, chat, user)
    assert "Ответ обрезан" in result.reply
    db.refresh(chat)
    assert all(block["type"] == "text" for message in engine._build_history(db, chat) for block in message["content"])
    handler.assert_not_called()


def test_missing_ai_key_does_not_create_an_abandoned_chat(db, api, make_user):
    from app.ai.models import Chat
    response = api(make_user(Module.AI)).post("/api/ai/chat/ask", json={"message": "Привет"})
    assert response.status_code == 503
    assert db.query(Chat).count() == 0
