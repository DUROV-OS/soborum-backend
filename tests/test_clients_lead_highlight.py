"""Подсветка заявки с сайта, уведомление и правка базовых данных лида (0111-b)."""

from datetime import datetime, timedelta, timezone

import pytest

from app.clients import intake_auth
from app.clients.models import Client, ClientChatLink, ClientNote, ClientStage
from app.common.module_access import AccessLevel, Module
from app.core.config import settings
from app.notifications.models import Notification

TOKEN = "test-intake-token"


@pytest.fixture(autouse=True)
def _intake_settings(monkeypatch):
    monkeypatch.setattr(settings, "lead_intake_token", TOKEN)
    monkeypatch.setattr(settings, "lead_intake_rate_limit", 100)
    intake_auth.reset_rate_limit()
    yield
    intake_auth.reset_rate_limit()


@pytest.fixture
def editor(make_user):
    return make_user(Module.CLIENTS, level=AccessLevel.EDIT)


def _intake(web, name="Тест Тестов", phone="+7 900 111-22-33", message="Хочу дом", **extra):
    body = {"name": name, "phone": phone, "message": message, **extra}
    response = web.post("/api/clients/intake", json=body, headers={"X-Intake-Token": TOKEN})
    assert response.status_code == 200, response.text
    return response.json()["client_id"]


def _notifications(db, client_id):
    return db.query(Notification).filter(Notification.object_type == "client", Notification.object_id == client_id).all()


def test_site_lead_is_flagged_and_exposes_request(api, editor, db):
    web = api(editor)
    client_id = _intake(web, source="Форма «Каталог»")

    card = web.get(f"/api/clients/{client_id}").json()

    assert card["lead_unprocessed"] is True
    assert card["lead_message"] == "Хочу дом"
    assert card["lead_source"] == "Форма «Каталог»"


def test_manual_lead_is_not_flagged(api, editor):
    web = api(editor)
    created = web.post(
        "/api/clients/", json={"full_name": "Иван", "phone": "+7 900 000-00-01", "email": "i@example.com"}
    ).json()

    assert created["lead_unprocessed"] is False
    assert created["lead_message"] is None


def test_take_clears_flag_and_repeat_request_raises_it_again(api, editor):
    web = api(editor)
    client_id = _intake(web)

    taken = web.post(f"/api/clients/{client_id}/take").json()
    assert taken["lead_unprocessed"] is False

    assert _intake(web, message="Ещё вопрос") == client_id
    card = web.get(f"/api/clients/{client_id}").json()
    assert card["lead_unprocessed"] is True
    assert card["lead_message"] == "Ещё вопрос"


def test_take_requires_edit_access(api, make_user):
    viewer = make_user(Module.CLIENTS, level=AccessLevel.VIEW)
    web = api(viewer)
    client_id = _intake(web)

    assert web.post(f"/api/clients/{client_id}/take").status_code == 403


def test_follow_up_task_clears_flag(api, editor):
    web = api(editor)
    client_id = _intake(web)

    deadline = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    response = web.post(f"/api/clients/{client_id}/tasks", json={"title": "Позвонить", "deadline": deadline})
    assert response.status_code == 201, response.text

    assert web.get(f"/api/clients/{client_id}").json()["lead_unprocessed"] is False


def test_stage_change_clears_flag(api, editor):
    web = api(editor)
    client_id = _intake(web)

    moved = web.post(f"/api/clients/{client_id}/transition")

    assert moved.status_code == 200, moved.text
    assert moved.json()["stage"] == ClientStage.DISCUSSION.value
    assert moved.json()["lead_unprocessed"] is False


def test_message_to_client_chat_clears_flag(api, editor, db, monkeypatch):
    from app.max import service as max_service

    monkeypatch.setattr(max_service, "send_message", lambda chat_id, text, notify=True: {"ok": True})
    web = api(editor)
    client_id = _intake(web)
    db.add(ClientChatLink(client_id=client_id, max_chat_id=555, label="Чат"))
    db.commit()

    assert web.post("/api/max/messages", json={"chat_id": 555, "text": "Здравствуйте"}).status_code == 201

    assert web.get(f"/api/clients/{client_id}").json()["lead_unprocessed"] is False


def test_message_to_foreign_chat_keeps_flag(api, editor, db, monkeypatch):
    from app.max import service as max_service

    monkeypatch.setattr(max_service, "send_message", lambda chat_id, text, notify=True: {"ok": True})
    web = api(editor)
    client_id = _intake(web)

    assert web.post("/api/max/messages", json={"chat_id": 999, "text": "привет"}).status_code == 201

    assert web.get(f"/api/clients/{client_id}").json()["lead_unprocessed"] is True


def test_notification_goes_to_manager_only(api, editor, make_user, db):
    manager = make_user(Module.CLIENTS, level=AccessLevel.EDIT)
    other = make_user(Module.CLIENTS, level=AccessLevel.EDIT)
    web = api(editor)
    client_id = _intake(web, name="Анна", phone="+7 900 222-33-44")
    # менеджер назначается после первого обращения — следующая заявка идёт ему одному
    web.patch(f"/api/clients/{client_id}/manager", json={"manager_id": manager.id})
    db.query(Notification).delete()
    db.commit()

    _intake(web, name="Анна", phone="+7 900 222-33-44", message="Ещё раз")

    rows = _notifications(db, client_id)
    assert [n.user_id for n in rows] == [manager.id]
    assert rows[0].title == "Повторная заявка с сайта: Анна"
    assert other.id not in [n.user_id for n in rows]


def test_notification_without_manager_goes_to_editors_not_viewers(api, make_user, db):
    editor1 = make_user(Module.CLIENTS, level=AccessLevel.EDIT)
    editor2 = make_user(Module.CLIENTS, level=AccessLevel.FULL)
    viewer = make_user(Module.CLIENTS, level=AccessLevel.VIEW)
    outsider = make_user(Module.WAREHOUSE, level=AccessLevel.EDIT)
    web = api(editor1)

    client_id = _intake(web, name="Пётр", message="Хочу баню")

    rows = _notifications(db, client_id)
    recipients = {n.user_id for n in rows}
    assert {editor1.id, editor2.id} <= recipients
    assert viewer.id not in recipients and outsider.id not in recipients
    assert all(n.title == "Новая заявка с сайта: Пётр" and n.body == "Хочу баню" for n in rows)


def test_update_basic_changes_fields_and_writes_history(api, editor, db):
    web = api(editor)
    client_id = _intake(web, name="Без почты", phone="+7 900 333-44-55")

    response = web.patch(
        f"/api/clients/{client_id}/basic",
        json={"email": "new@example.com", "contacts": [{"messenger": "max", "contact": "@ivan"}]},
    )

    assert response.status_code == 200, response.text
    card = response.json()
    assert card["email"] == "new@example.com" and card["phone"] == "+7 900 333-44-55"
    assert card["contacts"] == [{"messenger": "max", "contact": "@ivan"}]
    note = db.query(ClientNote).filter(ClientNote.client_id == client_id).one()
    assert note.author_id == editor.id
    assert "Почта" in note.text and "Способы связи" in note.text


def test_update_basic_without_changes_writes_no_history(api, editor, db):
    web = api(editor)
    client_id = _intake(web, name="Тот же", phone="+7 900 444-55-66")

    assert web.patch(f"/api/clients/{client_id}/basic", json={"full_name": "Тот же"}).status_code == 200

    assert db.query(ClientNote).filter(ClientNote.client_id == client_id).count() == 0


def test_update_basic_duplicate_phone_of_other_client_is_409(api, editor, db):
    web = api(editor)
    first = _intake(web, name="Первый", phone="+7 900 555-66-77")
    second = _intake(web, name="Второй", phone="+7 900 777-88-99")

    response = web.patch(f"/api/clients/{second}/basic", json={"phone": "8 (900) 555-66-77"})

    assert response.status_code == 409
    assert "Первый" in response.json()["detail"]
    assert db.get(Client, second).phone == "+7 900 777-88-99"
    assert first != second


def test_update_basic_own_contacts_are_not_a_duplicate(api, editor):
    web = api(editor)
    client_id = _intake(web, name="Сам", phone="+7 900 666-77-88")

    response = web.patch(f"/api/clients/{client_id}/basic", json={"phone": "8 900 666 77 88", "full_name": "Сам Себе"})

    assert response.status_code == 200, response.text


def test_update_basic_validation(api, editor, db):
    web = api(editor)
    client_id = _intake(web, name="Валидный", phone="+7 900 888-99-00")
    url = f"/api/clients/{client_id}/basic"

    assert web.patch(url, json={"full_name": "   "}).status_code == 422
    assert web.patch(url, json={"phone": ""}).status_code == 422  # почты нет — остался бы без связи
    assert web.patch(url, json={"phone": "abc"}).status_code == 422
    assert web.patch(url, json={"email": "не-почта"}).status_code == 422
    client = db.get(Client, client_id)
    assert client.full_name == "Валидный" and client.phone == "+7 900 888-99-00"

    # телефон можно убрать, если есть почта
    assert web.patch(url, json={"email": "ok@example.com"}).status_code == 200
    assert web.patch(url, json={"phone": ""}).status_code == 200
    assert db.get(Client, client_id).phone == ""


def test_update_basic_requires_edit_and_existing_client(api, make_user, editor):
    viewer = make_user(Module.CLIENTS, level=AccessLevel.VIEW)
    web = api(editor)
    client_id = _intake(web)

    assert api(viewer).patch(f"/api/clients/{client_id}/basic", json={"full_name": "Х"}).status_code == 403
    assert api(editor).patch("/api/clients/99999/basic", json={"full_name": "Х"}).status_code == 404
