"""0089-c: Marina's loop, streaming, web search and the knowledge-base MCP
connector run through the OpenAI adapter unchanged."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.ai import engine
from app.ai.models import ChatDomain, ChatMode
from app.ai.tools import TOOLS
from app.common.module_access import Module
from app.core import llm
from app.core.config import settings
from test_llm_openai_adapter import _call_item, _response, _text_item
from test_pilot_regressions import make_chat


class _ScriptedResponses:
    """Fake openai.responses: returns the next scripted answer per call -
    a Response for create(), a list of stream events for create(stream=True)."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        answer = self.answers.pop(0)
        return iter(answer) if kwargs.get("stream") else answer


def _ev(type_, **fields):
    return SimpleNamespace(type=type_, **fields)


def _openai(monkeypatch, *answers) -> _ScriptedResponses:
    fake = _ScriptedResponses(*answers)
    monkeypatch.setattr(settings, "ai_provider", "openai")
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(llm, "openai_client", lambda **_: SimpleNamespace(responses=fake))
    return fake


def test_server_tools_and_mcp_servers_translate_to_openai_tools():
    request = llm.to_openai_request(
        model="gpt-5", max_tokens=100, messages=[{"role": "user", "content": "x"}],
        tools=[
            {"type": "web_search_20260209", "name": "web_search", "max_uses": 2},
            {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 2},
        ],
        mcp_servers=[{
            "type": "url", "url": "https://kb.invalid/mcp", "name": "knowledge-base",
            "authorization_token": "tok", "tool_configuration": {"allowed_tools": engine.MCP_READ_ONLY_TOOLS},
        }],
    )
    assert request["tools"] == [
        {"type": "web_search"},
        {"type": "mcp", "server_label": "knowledge-base", "server_url": "https://kb.invalid/mcp",
         "require_approval": "never", "authorization": "tok", "allowed_tools": engine.MCP_READ_ONLY_TOOLS},
    ]


def test_admin_chat_sends_read_only_knowledge_base_to_openai(db, make_user, monkeypatch):
    fake = _openai(monkeypatch, _response([_text_item("ok")]))
    monkeypatch.setattr(settings, "mcp_server_url", "https://kb.invalid/mcp")
    monkeypatch.setattr(settings, "mcp_oauth_client_id", "fixture")
    monkeypatch.setattr(settings, "mcp_oauth_client_secret", "fixture")
    monkeypatch.setattr(engine.mcp_auth, "get_access_token", Mock(return_value="tok"))
    user = make_user(Module.AI, admin=True)

    engine._call_claude(db, "system", [{"role": "user", "content": "x"}], [], ChatMode.AUTO_APPROVE, user)

    (mcp,) = [t for t in fake.calls[0]["tools"] if t["type"] == "mcp"]
    assert mcp["allowed_tools"] == engine.MCP_READ_ONLY_TOOLS
    assert mcp["require_approval"] == "never"
    assert fake.calls[0]["model"] == settings.openai_model


def test_tool_loop_runs_on_openai_and_replays_the_call(db, make_user, monkeypatch):
    user = make_user(Module.AI, Module.CLIENTS)
    chat = make_chat(db, user, mode=ChatMode.AUTO_APPROVE, domain=ChatDomain.CLIENTS)
    monkeypatch.setattr(TOOLS["list_clients"], "handler", Mock(return_value=[{"id": 7, "name": "Иванов"}]))
    fake = _openai(
        monkeypatch,
        _response([_call_item("list_clients", "{}", call_id="call_42")]),
        _response([_text_item("Один клиент: Иванов.")]),
    )

    result = engine.run_turn(db, chat, user, "Кто у нас в клиентах?")

    assert result.status == "completed"
    assert result.reply == "Один клиент: Иванов."
    # The second request carries the call and its result as Responses items.
    replay = fake.calls[1]["input"]
    assert {"type": "function_call", "call_id": "call_42", "name": "list_clients", "arguments": "{}"} in replay
    (output,) = [i for i in replay if i.get("type") == "function_call_output"]
    assert output["call_id"] == "call_42" and "Иванов" in output["output"]
    # History stays in the Anthropic block format in the database.
    db.refresh(chat)
    assert chat.messages[1].content[0]["type"] == "tool_use"


def test_streaming_turn_on_openai_yields_status_text_and_persists_answer(db, make_user, monkeypatch):
    user = make_user(Module.AI, Module.CLIENTS)
    chat = make_chat(db, user, mode=ChatMode.AUTO_APPROVE, domain=ChatDomain.CLIENTS)
    final = _response([_text_item("Курс найден.")])
    _openai(monkeypatch, [
        _ev("response.created", response=None),
        _ev("response.output_item.added", item=SimpleNamespace(type="web_search_call")),
        _ev("response.output_item.done", item=SimpleNamespace(type="web_search_call")),
        _ev("response.output_item.added", item=SimpleNamespace(type="message")),
        _ev("response.output_text.delta", delta="Курс "),
        _ev("response.output_text.delta", delta="найден."),
        _ev("response.output_item.done", item=SimpleNamespace(type="message")),
        _ev("response.completed", response=final),
    ])
    engine.prepare_stream_turn(db, chat, user, "Какой курс юаня?")

    events = list(engine._advance_stream(db, chat, user))

    assert events == [
        {"type": "status", "text": "Ищу в интернете…"},
        {"type": "block_start"},
        {"type": "text", "text": "Курс "},
        {"type": "text", "text": "найден."},
        {"type": "block_end"},
        {"type": "done", "chat_id": chat.id},
    ]
    db.refresh(chat)
    assert chat.messages[-1].content == [{"type": "text", "text": "Курс найден."}]


def test_failed_openai_stream_surfaces_as_provider_error(monkeypatch):
    fake = _ScriptedResponses([
        _ev("response.failed", response=SimpleNamespace(error=SimpleNamespace(message="rate limited"))),
    ])
    client = llm.OpenAIMessagesClient(SimpleNamespace(responses=fake))
    with pytest.raises(llm.LLM_ERRORS):
        with client.messages.stream(model="gpt-5", max_tokens=10, messages=[{"role": "user", "content": "x"}]) as s:
            list(s)


def test_mcp_call_in_stream_shows_knowledge_base_status():
    fake = _ScriptedResponses([
        _ev("response.output_item.added",
            item=SimpleNamespace(type="mcp_call", server_label="knowledge-base", name="search_notes")),
        _ev("response.completed", response=_response([_text_item("ok")])),
    ])
    client = llm.OpenAIMessagesClient(SimpleNamespace(responses=fake))
    with client.beta.messages.stream(betas=["x"], mcp_servers=[], model="gpt-5", max_tokens=10,
                                     messages=[{"role": "user", "content": "x"}]) as stream:
        (start,) = list(stream)
        assert engine._tool_label(start.content_block) == "Смотрю базу знаний…"
        assert stream.get_final_message().content[0].text == "ok"
