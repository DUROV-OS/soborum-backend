"""Приём заявок с сайта вебхуком (0111-a)."""

import pytest
from fastapi.testclient import TestClient

from app.clients import intake_auth
from app.clients.models import Client, ClientLeadRequest, ClientStage
from app.core.config import settings
from app.db.session import get_db
from app.main import app

TOKEN = "test-intake-token"
URL = "/api/clients/intake"


@pytest.fixture
def intake(db, monkeypatch):
    """Вебхук без авторизации сотрудника: JWT не подменяем, только базу."""
    monkeypatch.setattr(settings, "lead_intake_token", TOKEN)
    monkeypatch.setattr(settings, "lead_intake_rate_limit", 20)
    intake_auth.reset_rate_limit()
    subapps = [r.app for r in app.routes if hasattr(r, "app") and hasattr(r.app, "dependency_overrides")]
    for subapp in subapps:
        subapp.dependency_overrides[get_db] = lambda: db
    yield TestClient(app)
    for subapp in subapps:
        subapp.dependency_overrides.clear()
    intake_auth.reset_rate_limit()


def _post(client, body=None, token=TOKEN, **kwargs):
    headers = {"X-Intake-Token": token} if token else {}
    return client.post(URL, json=body, headers=headers, **kwargs)


def test_creates_lead_with_phone_only(intake, db):
    response = _post(intake, {"name": " Тест Тестов ", "phone": "8 900 111-22-33", "message": "Хочу дом"})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "created"
    client = db.get(Client, body["client_id"])
    assert client.stage == ClientStage.LEAD
    assert client.full_name == "Тест Тестов"
    assert client.phone == "8 900 111-22-33"
    assert client.email == ""
    assert client.lead_source == "Сайт"
    [req] = client.lead_requests
    assert (req.message, req.source, req.is_repeat) == ("Хочу дом", "Сайт", False)


def test_creates_lead_with_email_only_and_utm(intake, db):
    response = _post(
        intake,
        {"name": "Анна", "email": "Anna@Example.com", "source": "Форма «Каталог»", "utm": {"utm_source": "ya"}},
    )

    client = db.get(Client, response.json()["client_id"])
    assert (client.phone, client.email, client.lead_source) == ("", "Anna@Example.com", "Форма «Каталог»")
    assert client.lead_requests[0].utm == {"utm_source": "ya"}


def test_duplicate_by_phone_adds_repeat_request(intake, db):
    first = _post(intake, {"name": "Тест", "phone": "+7 900 111-22-33", "message": "Первая"}).json()
    second = _post(intake, {"name": "Другой", "phone": "89001112233", "message": "Вторая"}).json()

    assert second == {"status": "duplicate", "client_id": first["client_id"]}
    assert db.query(Client).count() == 1
    requests = db.query(ClientLeadRequest).order_by(ClientLeadRequest.id).all()
    assert [(r.message, r.is_repeat) for r in requests] == [("Первая", False), ("Вторая", True)]


def test_duplicate_by_email_ignores_case(intake, db):
    first = _post(intake, {"name": "Анна", "email": "anna@example.com"}).json()
    second = _post(intake, {"name": "Анна", "email": "ANNA@example.com", "phone": "+7 999 000-00-00"}).json()

    assert second["status"] == "duplicate"
    assert second["client_id"] == first["client_id"]


def test_empty_contacts_do_not_merge_different_leads(intake, db):
    _post(intake, {"name": "Один", "phone": "+7 900 111-22-33"})
    second = _post(intake, {"name": "Два", "email": "two@example.com"}).json()

    assert second["status"] == "created"
    assert db.query(Client).count() == 2


def test_manual_client_is_matched_as_duplicate(intake, db, api, make_user):
    from app.common.module_access import AccessLevel, Module

    manager = make_user(Module.CLIENTS, level=AccessLevel.EDIT)
    created = api(manager).post(
        "/api/clients/", json={"full_name": "Иван", "phone": "+7 900 111-22-33", "email": "i@example.com"}
    ).json()

    result = _post(TestClient(app), {"name": "Иван", "phone": "8 (900) 111-22-33"}, token=TOKEN).json()

    assert result == {"status": "duplicate", "client_id": created["id"]}


def test_token_checks(intake, db):
    assert _post(intake, {"name": "А", "phone": "123456"}, token=None).status_code == 401
    assert _post(intake, {"name": "А", "phone": "123456"}, token="wrong-token").status_code == 401
    assert db.query(Client).count() == 0


def test_token_in_query_and_form_for_tilda(intake, db):
    by_query = intake.post(f"{URL}?token={TOKEN}", data={"Name": "Тильда", "Phone": "+7 900 000-00-01", "Email": ""})
    assert by_query.status_code == 200 and by_query.json()["status"] == "created"

    by_field = intake.post(URL, data={"token": TOKEN, "name": "Поле", "phone": "+7 900 000-00-02", "utm_source": "tilda"})
    assert by_field.status_code == 200
    assert db.get(Client, by_field.json()["client_id"]).lead_requests[0].utm == {"utm_source": "tilda"}
    assert intake.post(URL, data={"name": "Без", "phone": "+7 900 000-00-03"}).status_code == 401


def test_form_body_keeps_cyrillic_both_encoded_and_raw(intake, db):
    encoded = intake.post(
        f"{URL}?token={TOKEN}",
        content="Name=%D0%A2%D0%B8%D0%BB%D1%8C%D0%B4%D0%B0&Phone=%2B7+900+000-00-11",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    raw = intake.post(
        f"{URL}?token={TOKEN}",
        content="Name=Тильда&Phone=+7 900 000-00-12".encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    assert db.get(Client, encoded.json()["client_id"]).full_name == "Тильда"
    assert db.get(Client, raw.json()["client_id"]).full_name == "Тильда"


def test_validation_requires_name_and_contact(intake, db):
    assert _post(intake, {"name": "Без контактов"}).status_code == 422
    assert _post(intake, {"name": "Мусор", "phone": "abc"}).status_code == 422
    assert _post(intake, {"phone": "+7 900 111-22-33"}).status_code == 422
    assert _post(intake, {"name": "Почта", "email": "не-почта"}).status_code == 422
    assert db.query(Client).count() == 0


def test_not_configured_returns_503(intake, monkeypatch):
    monkeypatch.setattr(settings, "lead_intake_token", "")

    assert _post(intake, {"name": "А", "phone": "123456"}).status_code == 503
    assert _post(intake, {"name": "А", "phone": "123456"}, token="").status_code == 503


def test_rate_limit(intake, monkeypatch):
    monkeypatch.setattr(settings, "lead_intake_rate_limit", 3)
    intake_auth.reset_rate_limit()

    codes = [_post(intake, {"name": "А", "phone": f"+7 900 000-00-0{i}"}).status_code for i in range(5)]

    assert codes == [200, 200, 200, 429, 429]


def test_other_client_routes_still_need_login(intake):
    assert intake.get("/api/clients/").status_code in (401, 403)
