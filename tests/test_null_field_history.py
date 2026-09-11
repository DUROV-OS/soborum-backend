"""Регрессия: второе сообщение в чате падало с 400 от Anthropic
(«messages.1.content.0.text.parsed_output: Extra inputs are not permitted»).

Anthropic-SDK отдаёт в ответе блоки с response-only полями (`citations`,
`parsed_output`), которые приходят как явный `null`, когда не используются.
`content_blocks = [block.model_dump(mode="json") for block in response.content]`
сохранял их as-is; при повторной отправке той же истории на СЛЕДУЮЩЕМ ходу
Anthropic отклонял их как «лишние» поля во входной схеме — первый ответ
проходил, а любой второй в том же чате падал.

Фикс: `exclude_none=True` при дампе (не пишем null-поля впредь) +
`_drop_null_fields` в `_resolve_content` (лечит уже испорченные чаты при
каждой пересборке истории, без миграции)."""

from types import SimpleNamespace
from unittest.mock import Mock

from app.ai import engine
from app.ai.models import ChatDomain, ChatMode
from app.common.module_access import Module

from tests.test_pilot_regressions import make_chat


class _FakeBlock(SimpleNamespace):
    def model_dump(self, mode="json", exclude_none=False):
        data = dict(self.__dict__)
        return {k: v for k, v in data.items() if v is not None} if exclude_none else data


def _response_with_nulls(text: str):
    block = _FakeBlock(type="text", text=text, citations=None, parsed_output=None)
    return SimpleNamespace(stop_reason="end_turn", content=[block])


def test_persisted_assistant_message_has_no_null_fields(db, make_user, monkeypatch):
    user = make_user(Module.AI)
    chat = make_chat(db, user, ChatMode.REQUIRE_APPROVAL, ChatDomain.GENERAL)
    monkeypatch.setattr(engine, "_call_claude", Mock(return_value=_response_with_nulls("Всего 3 клиента.")))

    engine.run_turn(db, chat, user, "Сколько клиентов?")

    db.refresh(chat)
    assistant_message = next(m for m in chat.messages if m.role == "assistant")
    assert assistant_message.content == [{"type": "text", "text": "Всего 3 клиента."}]


def test_resolve_content_heals_already_corrupted_history(db):
    """Чат, испорченный ДО фикса (уже с citations/parsed_output: null в БД),
    должен собираться в валидную для Anthropic историю без этих полей."""
    corrupted = [{"type": "text", "text": "Ответ", "citations": None, "parsed_output": None}]

    resolved = engine._resolve_content(db, corrupted)

    assert resolved == [{"type": "text", "text": "Ответ"}]


def test_second_turn_in_same_chat_does_not_resend_null_fields(db, make_user, monkeypatch):
    """Сквозной сценарий бага: первый ход отвечает с null-полями, второй ход
    в том же чате не должен передавать их обратно в Anthropic."""
    user = make_user(Module.AI)
    chat = make_chat(db, user, ChatMode.REQUIRE_APPROVAL, ChatDomain.GENERAL)
    call_claude = Mock(side_effect=[
        _response_with_nulls("Всего 3 клиента."),
        _response_with_nulls("Из них 3 на стадии «лид»."),
    ])
    monkeypatch.setattr(engine, "_call_claude", call_claude)

    engine.run_turn(db, chat, user, "Сколько клиентов?")
    db.refresh(chat)
    engine.run_turn(db, chat, user, "А на стадии лид?")

    second_call_history = call_claude.call_args_list[1].args[2]
    for message in second_call_history:
        for block in message["content"]:
            assert "citations" not in block
            assert "parsed_output" not in block
