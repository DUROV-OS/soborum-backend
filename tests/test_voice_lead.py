"""Регрессия: «Голосом: …» протекало сырым текстом в обычный чат «Марина» →
«Общий», потому что и он, и «Совещание»/«Агенты → Консультация» использовали
один и тот же системный промпт ChatDomain.GENERAL с голосовым форматом —
а фронт обычного чата (ChatPanel/MessageBubble) его не разбирает, в отличие
от ConsultPanel и «Совещания». Теперь формат добавляется только там, где
ответ действительно озвучивается (voice_lead=True)."""

from unittest.mock import Mock

from app.ai import engine
from app.ai.models import ChatDomain, ChatMode
from app.common.module_access import Module

from tests.test_pilot_regressions import final_response, make_chat


def test_general_system_prompt_has_no_voice_format_by_default(db, make_user):
    user = make_user(Module.AI)
    chat = make_chat(db, user, ChatMode.REQUIRE_APPROVAL, ChatDomain.GENERAL)

    system = engine._system_for(chat, db)

    assert "Голосом" not in system


def test_general_system_prompt_gets_voice_format_when_requested(db, make_user):
    user = make_user(Module.AI)
    chat = make_chat(db, user, ChatMode.REQUIRE_APPROVAL, ChatDomain.GENERAL)

    system = engine._system_for(chat, db, voice_lead=True)

    assert "Голосом" in system


def test_plain_chat_ask_does_not_request_voice_format(db, make_user, monkeypatch):
    """Обычный /chat/ask (домен general в AiChatPage) идёт через run_turn без
    voice_lead — Claude не получает инструкцию про «Голосом: …»."""
    user = make_user(Module.AI)
    chat = make_chat(db, user, ChatMode.REQUIRE_APPROVAL, ChatDomain.GENERAL)
    call_claude = Mock(return_value=final_response())
    monkeypatch.setattr(engine, "_call_claude", call_claude)

    engine.run_turn(db, chat, user, "Сколько клиентов в системе?")

    system_sent = call_claude.call_args.args[1]
    assert "Голосом" not in system_sent


def test_consult_ask_requests_voice_format(db, make_user, monkeypatch):
    """/consult/ask (Агенты → Консультация) явно передаёт voice_lead=True —
    фронт там разбирает «Голосом: …» и озвучивает его."""
    user = make_user(Module.AI)
    chat = make_chat(db, user, ChatMode.REQUIRE_APPROVAL, ChatDomain.GENERAL)
    call_claude = Mock(return_value=final_response())
    monkeypatch.setattr(engine, "_call_claude", call_claude)

    engine.run_turn(db, chat, user, "Сколько клиентов в системе?", voice_lead=True)

    system_sent = call_claude.call_args.args[1]
    assert "Голосом" in system_sent
