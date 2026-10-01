"""Задача 0074: смена роли через PATCH /api/auth/users/{id}.

Администратор назначает и снимает админские права другим, но не себе, и не
может оставить систему без единственного активного администратора."""

from app.common.module_access import AccessLevel, Module
from app.users.models import User, UserModuleAccess, UserRole


def test_admin_promotes_worker_and_grants_are_dropped(api, make_user, db):
    admin = make_user(admin=True)
    other_admin = make_user(admin=True)
    worker = make_user(Module.WAREHOUSE, level=AccessLevel.VIEW)
    assert other_admin.role == UserRole.ADMIN

    response = api(admin).patch(f"/api/auth/users/{worker.id}", json={"role": "admin"})

    assert response.status_code == 200
    assert response.json()["role"] == "admin"
    assert response.json()["module_access"][Module.WAREHOUSE.value] == AccessLevel.FULL.value
    db.refresh(worker)
    assert worker.role == UserRole.ADMIN
    assert db.query(UserModuleAccess).filter(UserModuleAccess.user_id == worker.id).count() == 0


def test_demoted_admin_has_no_module_access_left(api, make_user, db):
    admin = make_user(admin=True)
    target = make_user(Module.WAREHOUSE, level=AccessLevel.EDIT)
    api(admin).patch(f"/api/auth/users/{target.id}", json={"role": "admin"})

    response = api(admin).patch(f"/api/auth/users/{target.id}", json={"role": "worker"})

    assert response.status_code == 200
    assert response.json()["role"] == "worker"
    assert set(response.json()["module_access"].values()) == {AccessLevel.NONE.value}


def test_admin_cannot_change_own_role(api, make_user):
    admin = make_user(admin=True)
    make_user(admin=True)

    response = api(admin).patch(f"/api/auth/users/{admin.id}", json={"role": "worker"})

    assert response.status_code == 400
    assert response.json()["detail"] == "Нельзя изменить собственную роль"


def test_admin_cannot_disable_own_account(api, make_user):
    admin = make_user(admin=True)
    make_user(admin=True)

    response = api(admin).patch(f"/api/auth/users/{admin.id}", json={"is_active": False})

    assert response.status_code == 400
    assert response.json()["detail"] == "Нельзя отключить собственную учётную запись"


def test_last_admin_cannot_be_demoted(api, make_user, db):
    admin = make_user(admin=True)
    only_other = make_user(admin=True)
    # Оставляем в системе ровно одного активного админа — `only_other`.
    admin.is_active = False
    db.commit()

    response = api(admin).patch(f"/api/auth/users/{only_other.id}", json={"role": "worker"})

    assert response.status_code == 400
    assert response.json()["detail"] == "В системе должен остаться хотя бы один администратор"
    db.refresh(only_other)
    assert only_other.role == UserRole.ADMIN


def test_last_admin_cannot_be_disabled(api, make_user, db):
    admin = make_user(admin=True)
    only_other = make_user(admin=True)
    admin.is_active = False
    db.commit()

    response = api(admin).patch(f"/api/auth/users/{only_other.id}", json={"is_active": False})

    assert response.status_code == 400
    db.refresh(only_other)
    assert only_other.is_active is True


def test_worker_cannot_promote_anyone(api, make_user):
    worker = make_user(Module.WAREHOUSE)
    other = make_user(Module.WAREHOUSE)

    response = api(worker).patch(f"/api/auth/users/{other.id}", json={"role": "admin"})

    assert response.status_code == 403
    assert isinstance(other, User)
