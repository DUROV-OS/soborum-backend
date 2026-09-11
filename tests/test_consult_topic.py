from app.ai.models import Chat, ChatDomain, ChatMode, Message
from app.ai.service import user_texts, wipe_chat
from app.ai.topic import topic_shifted
from app.common.module_access import Module


def test_topic_keeps_follow_up_on_same_cluster():
    previous = ["Какие позиции на складе в минусе по вентиляторам?"]
    assert topic_shifted(previous, "А саморезы тоже ушли в минус?") is False


def test_topic_resets_when_cluster_changes():
    previous = ["Какие позиции на складе в минусе?"]
    assert topic_shifted(previous, "Сколько стоит барн 96 в ипотеку для клиента?") is True


def test_topic_keeps_short_ack():
    previous = ["Какие сделки зависли без цены?"]
    assert topic_shifted(previous, "Ок, понял") is False


def test_consult_wipes_server_transcript(db, make_user):
    user = make_user()
    chat = Chat(owner_id=user.id, domain=ChatDomain.GENERAL, mode=ChatMode.REQUIRE_APPROVAL)
    db.add(chat)
    db.commit()
    db.refresh(chat)
    db.add(Message(chat_id=chat.id, role="user", content=[{"type": "text", "text": "Склад в минусе?"}]))
    db.commit()
    db.refresh(chat)
    assert user_texts(chat) == ["Склад в минусе?"]
    wipe_chat(db, chat)
    assert db.get(Chat, chat.id) is None


def test_consult_ask_requires_login_not_ai_module(api, make_user, monkeypatch):
    user = make_user()  # worker without Module.AI
    monkeypatch.setattr("app.core.config.settings.anthropic_api_key", "sk-test")

    def fake_turn(db, chat, owner, message, file_ids=None, **_kwargs):
        from app.ai.engine import TurnResult

        return TurnResult(status="completed", reply="Кратко: смотрю факт.")

    monkeypatch.setattr("app.ai.engine.run_turn", fake_turn)
    response = api(user).post("/api/ai/consult/ask", json={"message": "Что горит на складе?"})
    assert response.status_code == 200
    assert response.json()["reply"] == "Кратко: смотрю факт."
    assert response.json()["topic_reset"] is False


def test_consult_ask_wipes_old_chat_on_topic_change(api, db, make_user, monkeypatch):
    user = make_user(Module.AI, admin=True)
    chat = Chat(owner_id=user.id, domain=ChatDomain.GENERAL, mode=ChatMode.REQUIRE_APPROVAL)
    db.add(chat)
    db.commit()
    db.refresh(chat)
    db.add(Message(chat_id=chat.id, role="user", content=[{"type": "text", "text": "Что в минусе на складе?"}]))
    db.commit()
    monkeypatch.setattr("app.core.config.settings.anthropic_api_key", "sk-test")
    monkeypatch.setattr(
        "app.ai.engine.run_turn",
        lambda *args, **kwargs: __import__("app.ai.engine", fromlist=["TurnResult"]).TurnResult(
            status="completed", reply="Про ипотеку отдельно."
        ),
    )
    response = api(user).post(
        "/api/ai/consult/ask",
        json={"chat_id": chat.id, "message": "Сколько стоит барн 96 в ипотеку для клиента?"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["topic_reset"] is True
    assert db.query(Message).count() == 0
    assert db.get(Chat, body["chat_id"]) is not None


def test_worker_without_ai_cannot_use_old_marina_ask(api, make_user):
    user = make_user()
    response = api(user).post("/api/ai/chat/ask", json={"message": "Привет"})
    assert response.status_code == 403
