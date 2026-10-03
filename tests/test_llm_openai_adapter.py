"""0089-b: the OpenAI adapter speaks Anthropic messages.create on both ends."""

from types import SimpleNamespace

from anthropic.types import Message
from openai.types.responses import Response, ResponseFunctionToolCall, ResponseOutputMessage

from app.core import llm
from app.core.config import settings


def _text_item(text: str) -> ResponseOutputMessage:
    return ResponseOutputMessage.model_validate({
        "id": "msg_1", "type": "message", "role": "assistant", "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    })


def _call_item(name: str, arguments: str, call_id: str = "call_1") -> ResponseFunctionToolCall:
    return ResponseFunctionToolCall.model_validate({
        "type": "function_call", "call_id": call_id, "name": name, "arguments": arguments, "status": "completed",
    })


def _response(output, *, status="completed", incomplete_reason=None) -> Response:
    return Response.model_construct(
        id="resp_1", model="gpt-5", output=output, status=status,
        incomplete_details=SimpleNamespace(reason=incomplete_reason) if incomplete_reason else None,
        usage=SimpleNamespace(input_tokens=11, output_tokens=7),
    )


class _FakeResponses:
    def __init__(self, response):
        self.response = response
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def _adapter(response):
    fake = _FakeResponses(response)
    return llm.OpenAIMessagesClient(SimpleNamespace(responses=fake)), fake


def test_llm_client_follows_ai_provider(monkeypatch):
    monkeypatch.setattr(settings, "ai_provider", "openai")
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    assert isinstance(llm.llm_client(), llm.OpenAIMessagesClient)
    monkeypatch.setattr(settings, "ai_provider", "anthropic")
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-test")
    assert not isinstance(llm.llm_client(), llm.OpenAIMessagesClient)


def test_empty_openai_base_url_falls_back_to_official_endpoint(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(settings, "openai_base_url", "")
    assert str(llm.openai_client().base_url).startswith("https://api.openai.com/v1")


def test_forced_tool_call_round_trip():
    client, fake = _adapter(_response([_call_item("submit", '{"items": [1, 2]}')]))
    tool = {"name": "submit", "description": "Отдать результат", "input_schema": {"type": "object"}}

    message = client.messages.create(
        model="gpt-5", max_tokens=1024, system="Ты помощник.",
        messages=[{"role": "user", "content": "Разбери таблицу"}],
        tools=[tool], tool_choice={"type": "tool", "name": "submit"},
    )

    sent = fake.calls[0]
    assert sent["instructions"] == "Ты помощник."
    assert sent["input"] == [{"role": "user", "content": [{"type": "input_text", "text": "Разбери таблицу"}]}]
    assert sent["tools"] == [{"type": "function", "name": "submit", "description": "Отдать результат",
                              "parameters": {"type": "object"}, "strict": False}]
    assert sent["tool_choice"] == {"type": "function", "name": "submit"}
    assert sent["store"] is False

    assert isinstance(message, Message)
    assert message.stop_reason == "tool_use"
    block = message.content[0]
    assert (block.type, block.name, block.input, block.id) == ("tool_use", "submit", {"items": [1, 2]}, "call_1")
    assert (message.usage.input_tokens, message.usage.output_tokens) == (11, 7)


def test_reasoning_model_gets_effort_and_token_headroom():
    _, fake = _adapter(_response([_text_item("ok")]))
    llm.OpenAIMessagesClient(SimpleNamespace(responses=fake)).messages.create(
        model="gpt-5", max_tokens=1024, messages=[{"role": "user", "content": "hi"}],
        output_config={"effort": "xhigh"},
    )
    llm.OpenAIMessagesClient(SimpleNamespace(responses=fake)).messages.create(
        model="gpt-4.1", max_tokens=1024, messages=[{"role": "user", "content": "hi"}],
    )
    reasoning_call, plain_call = fake.calls
    assert reasoning_call["reasoning"] == {"effort": "high"}
    assert reasoning_call["max_output_tokens"] > 1024
    assert "reasoning" not in plain_call
    assert plain_call["max_output_tokens"] == 1024


def test_history_with_tools_and_attachments_is_translated_in_order():
    history = [
        {"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAA"}},
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": "BBB"},
             "title": "смета.pdf"},
            {"type": "document", "source": {"type": "text", "media_type": "text/plain", "data": "строка"},
             "title": "notes.txt"},
            {"type": "text", "text": "Что тут?"},
        ]},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "...", "signature": "x"},
            {"type": "text", "text": "Посмотрю клиентов."},
            {"type": "tool_use", "id": "toolu_1", "name": "list_clients", "input": {"stage": "lead"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": '[{"id": 1}]', "is_error": False},
        ]},
    ]
    items = llm.to_openai_request(model="gpt-5", max_tokens=100, messages=history)["input"]

    user_parts = items[0]["content"]
    assert user_parts[0] == {"type": "input_image", "detail": "auto", "image_url": "data:image/png;base64,AAA"}
    assert user_parts[1] == {"type": "input_file", "filename": "смета.pdf",
                             "file_data": "data:application/pdf;base64,BBB"}
    assert user_parts[2] == {"type": "input_text", "text": "Файл «notes.txt»:\nстрока"}
    assert user_parts[3] == {"type": "input_text", "text": "Что тут?"}
    # thinking is dropped; text and the call keep their order
    assert items[1] == {"role": "assistant", "content": [{"type": "output_text", "text": "Посмотрю клиентов."}]}
    assert items[2] == {"type": "function_call", "call_id": "toolu_1", "name": "list_clients",
                        "arguments": '{"stage": "lead"}'}
    assert items[3] == {"type": "function_call_output", "call_id": "toolu_1", "output": '[{"id": 1}]'}


def test_error_tool_result_is_marked_for_the_model():
    items = llm.to_openai_request(model="gpt-5", max_tokens=100, messages=[{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "c1", "content": "нет доступа", "is_error": True},
    ]}])["input"]
    assert items == [{"type": "function_call_output", "call_id": "c1", "output": "ERROR: нет доступа"}]


def test_truncated_response_reports_max_tokens():
    client, _ = _adapter(_response([_text_item("Начало отв")], status="incomplete",
                                   incomplete_reason="max_output_tokens"))
    message = client.messages.create(model="gpt-5", max_tokens=10, messages=[{"role": "user", "content": "x"}])
    assert message.stop_reason == "max_tokens"
    assert message.content[0].text == "Начало отв"


def test_plain_answer_is_end_turn_and_dumps_like_claude():
    client, _ = _adapter(_response([_text_item("Готово")]))
    message = client.messages.create(model="gpt-5", max_tokens=10, messages=[{"role": "user", "content": "x"}])
    assert message.stop_reason == "end_turn"
    assert [b.model_dump(mode="json", exclude_none=True) for b in message.content] == [
        {"type": "text", "text": "Готово"}
    ]
