import pytest

from app.common.module_access import AccessLevel, Module
from app.users.models import User, UserModuleAccess, UserRole


def test_admin_sets_normalized_work_phone_without_changing_access(api, make_user, db):
    admin = make_user(admin=True)
    worker = make_user(Module.CLIENTS, Module.TASKS, level=AccessLevel.EDIT)
    worker.job_role = "manager"
    worker.workspace_preferences = {"view": "manager", "blocks": ["my_clients"], "sections": ["clients"]}
    db.commit()
    password_before = worker.hashed_password
    grants_before = [(grant.module, grant.level) for grant in worker.module_access]
    response = api(admin).patch(
        f"/api/auth/users/{worker.id}",
        json={"work_phone": "8 (900) 555-01-23", "telephony_extension": " 0012 "},
    )
    assert response.status_code == 200
    assert response.json()["work_phone"] == "+79005550123"
    assert response.json()["telephony_extension"] == "0012"
    db.expire_all()
    assert worker.work_phone == "+79005550123"
    assert worker.telephony_extension == "0012"
    assert worker.role == UserRole.WORKER and worker.is_active
    assert worker.job_role == "manager"
    assert worker.workspace_preferences["view"] == "manager"
    assert worker.hashed_password == password_before
    assert [(grant.module, grant.level) for grant in worker.module_access] == grants_before
    me = api(worker).get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["work_phone"] == "+79005550123"
    assert me.json()["telephony_extension"] == "0012"
    assert not any(key in me.json() for key in ("caller_id_verified", "telephony_connected", "telephony_token"))


def test_omitted_fields_are_kept_and_explicit_null_or_empty_clears(api, make_user, db):
    admin = make_user(admin=True)
    worker = make_user()
    worker.work_phone = "+12025550123"
    worker.telephony_extension = "120"
    db.commit()
    client = api(admin)
    assert client.patch(f"/api/auth/users/{worker.id}", json={"full_name": "Новое имя"}).status_code == 200
    assert worker.work_phone == "+12025550123" and worker.telephony_extension == "120"
    response = client.patch(f"/api/auth/users/{worker.id}", json={"work_phone": None})
    assert response.status_code == 200
    assert response.json()["work_phone"] is None and response.json()["telephony_extension"] == "120"
    response = client.patch(f"/api/auth/users/{worker.id}", json={"telephony_extension": "  "})
    assert response.status_code == 200
    assert response.json()["telephony_extension"] is None
    assert client.patch(f"/api/auth/users/{worker.id}", json={"work_phone": " "}).json()["work_phone"] is None


@pytest.mark.parametrize("phone,expected", [
    ("+7 (900) 555-01-23", "+79005550123"),
    ("79005550123", "+79005550123"),
    ("8 900 555 01 23", "+79005550123"),
    ("+1 202 555 0123", "+12025550123"),
    ("+44 20 7946 0958", "+442079460958"),
    (None, None),
])
def test_create_user_accepts_optional_normalized_numbers(api, make_user, db, phone, expected):
    admin = make_user(admin=True)
    response = api(admin).post("/api/auth/users", json={
        "email": "new.employee@example.com", "password": "synthetic-test-password", "full_name": "Сотрудник",
        "work_phone": phone, "telephony_extension": "005", "module_access": {"clients": "view"},
    })
    assert response.status_code == 201
    data = response.json()
    assert data["work_phone"] == expected and data["telephony_extension"] == "005"
    created = db.get(User, data["id"])
    assert created.work_phone == expected
    assert created.access_level(Module.CLIENTS) == AccessLevel.VIEW
    assert created.access_level(Module.PRODUCTION) == AccessLevel.NONE


@pytest.mark.parametrize("payload", [
    {"work_phone": "https://example.com/call"},
    {"work_phone": "tel:+79005550123"},
    {"work_phone": "+79005550123;123"},
    {"work_phone": "+79005550123 доб.12"},
    {"work_phone": "+00000000000"},
    {"work_phone": "+71234567"},
    {"work_phone": "+1234567890123456"},
    {"work_phone": "9005550123"},
    {"work_phone": "123"},
    {"work_phone": 79005550123},
    {"telephony_extension": "user@example.com"},
    {"telephony_extension": "sip:123"},
    {"telephony_extension": "12#3"},
    {"telephony_extension": "12345678901"},
    {"telephony_extension": "12\n34"},
    {"telephony_extension": 123},
])
def test_invalid_telephony_values_do_not_mutate_user(api, make_user, db, payload):
    admin = make_user(admin=True)
    worker = make_user(Module.CLIENTS)
    worker.work_phone = "+12025550123"
    worker.telephony_extension = "015"
    db.commit()
    response = api(admin).patch(f"/api/auth/users/{worker.id}", json=payload)
    assert response.status_code == 422
    db.refresh(worker)
    assert worker.work_phone == "+12025550123" and worker.telephony_extension == "015"
    assert worker.access_level(Module.CLIENTS) == AccessLevel.EDIT


def test_full_sales_access_cannot_set_self_or_other_employee_number(api, make_user, db):
    worker = make_user(Module.CLIENTS, level=AccessLevel.FULL)
    other = make_user(Module.TASKS)
    client = api(worker)
    for target in (worker, other):
        assert client.patch(f"/api/auth/users/{target.id}", json={"work_phone": "+12025550123"}).status_code == 403
    assert client.get("/api/auth/users").status_code == 403
    assert client.get(f"/api/auth/users/{other.id}").status_code == 403
    assert client.post("/api/auth/users", json={"email": "blocked@example.com", "password": "test-password", "full_name": "Blocked", "work_phone": "+12025550123"}).status_code == 403
    db.expire_all()
    assert worker.work_phone is None and other.work_phone is None
    me = client.get("/api/auth/me").json()
    assert me["work_phone"] is None and me["telephony_extension"] is None


def test_admin_can_set_own_contact_and_list_it_without_changing_role(api, make_user, db):
    admin = make_user(admin=True)
    client = api(admin)
    response = client.patch(f"/api/auth/users/{admin.id}", json={"work_phone": "+12025550123"})
    assert response.status_code == 200 and response.json()["role"] == "admin"
    assert client.get("/api/auth/users").json()[0]["work_phone"] == "+12025550123"
    assert db.query(UserModuleAccess).filter(UserModuleAccess.user_id == admin.id).count() == 0
    assert client.patch("/api/auth/users/999999", json={"work_phone": None}).status_code == 404


def test_invalid_create_does_not_create_account(api, make_user, db):
    admin = make_user(admin=True)
    before = db.query(User).count()
    response = api(admin).post("/api/auth/users", json={
        "email": "invalid@example.com", "password": "test-password", "full_name": "Invalid",
        "work_phone": "+12025550123", "telephony_extension": "not-a-number",
    })
    assert response.status_code == 422
    assert db.query(User).count() == before
