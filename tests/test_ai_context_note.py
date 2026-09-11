"""Регрессия 0017: [client_id=N] (и аналоги — cycle_id/production_id/module_id)
протекали в видимый текст диалога с Мариной, потому что фронт склеивал их прямо
в текст сообщения. Теперь такой контекст едет отдельным блоком контента
(`context_note`), который разворачивается в текст только при сборке истории
для Claude (app.ai.engine._resolve_content) и не попадает в то, что рендерит
MessageBubble на фронте (она понимает только "text"/"tool_use"/"file_ref")."""

from unittest.mock import Mock

from app.ai import engine
from app.ai.models import ChatDomain, ChatMode, Message
from app.common.module_access import Module

from tests.test_pilot_regressions import final_response, make_chat


def test_run_turn_stores_context_note_as_separate_block(db, make_user, monkeypatch):
    user = make_user(Module.AI, Module.CLIENTS)
    chat = make_chat(db, user, ChatMode.REQUIRE_APPROVAL, ChatDomain.CLIENTS)
    monkeypatch.setattr(engine, "_call_claude", Mock(return_value=final_response()))

    engine.run_turn(db, chat, user, "Какая стадия у клиента?",
                     context_note="[client_id=6, Иванов И.]")

    db.refresh(chat)
    user_message = next(m for m in chat.messages if m.role == "user")
    assert user_message.content == [
        {"type": "context_note", "note": "[client_id=6, Иванов И.]"},
        {"type": "text", "text": "Какая стадия у клиента?"},
    ]


def test_resolve_content_unfolds_context_note_into_text_for_claude(db):
    content = [
        {"type": "context_note", "note": "[client_id=6, Иванов И.]"},
        {"type": "text", "text": "Какая стадия у клиента?"},
    ]

    resolved = engine._resolve_content(db, content)

    assert resolved == [
        {"type": "text", "text": "[client_id=6, Иванов И.]"},
        {"type": "text", "text": "Какая стадия у клиента?"},
    ]
    # исходные (персистентные) блоки не мутируются — _resolve_content строит новый список
    assert content[0]["type"] == "context_note"


def test_build_history_gives_claude_the_context_while_keeping_it_out_of_the_stored_type(db, make_user):
    user = make_user(Module.AI, Module.CLIENTS)
    chat = make_chat(db, user, ChatMode.REQUIRE_APPROVAL, ChatDomain.CLIENTS)
    db.add(Message(chat_id=chat.id, role="user", content=[
        {"type": "context_note", "note": "[client_id=6, Иванов И.]"},
        {"type": "text", "text": "Какая стадия у клиента?"},
    ]))
    db.commit()
    db.refresh(chat)

    history = engine._build_history(db, chat)

    assert history == [{"role": "user", "content": [
        {"type": "text", "text": "[client_id=6, Иванов И.]"},
        {"type": "text", "text": "Какая стадия у клиента?"},
    ]}]
    # то, что реально хранится в БД, по-прежнему помечено как context_note —
    # именно это MessageBubble на фронте не рендерит
    db.refresh(chat)
    stored = next(m for m in chat.messages if m.role == "user")
    assert stored.content[0]["type"] == "context_note"


def test_run_turn_without_context_note_behaves_as_before(db, make_user, monkeypatch):
    user = make_user(Module.AI, Module.CLIENTS)
    chat = make_chat(db, user, ChatMode.REQUIRE_APPROVAL, ChatDomain.CLIENTS)
    monkeypatch.setattr(engine, "_call_claude", Mock(return_value=final_response()))

    engine.run_turn(db, chat, user, "Привет")

    db.refresh(chat)
    user_message = next(m for m in chat.messages if m.role == "user")
    assert user_message.content == [{"type": "text", "text": "Привет"}]
