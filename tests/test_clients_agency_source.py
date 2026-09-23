"""Источник клиента: пришёл сам или его привело агентство (0079-c).

- клиент создаётся прямым по умолчанию — отметки об агентстве раньше не было,
  и у всех ранее заведённых клиентов она пустая;
- с отметкой название агентства обязательно;
- снятие отметки чистит название и контакт, чтобы у прямого клиента не
  осталось хвоста от прошлой правки;
- источник, в отличие от ФИО/телефона/почты, правится после создания.
"""

import pytest
from fastapi import HTTPException

from app.clients import service as client_service
from app.clients.models import Client
from app.clients.schemas import ClientCreate, ClientSourceUpdate
from app.common.module_access import AccessLevel, Module


def _payload(**kwargs):
    base = dict(full_name="Иван Тест", phone="+70000000000", email="ivan@example.com")
    base.update(kwargs)
    return ClientCreate(**base)


def test_client_is_direct_by_default(db):
    client = client_service.create_client(db, _payload())
    db.commit()
    assert client.via_agency is False
    assert client.agency_name is None
    assert client.agency_contact is None


def test_agency_client_keeps_name_and_contact(db):
    client = client_service.create_client(
        db,
        _payload(via_agency=True, agency_name="  Агентство №1  ", agency_contact="+7 900 000-00-00"),
    )
    db.commit()
    assert client.via_agency is True
    assert client.agency_name == "Агентство №1"
    assert client.agency_contact == "+7 900 000-00-00"


def test_agency_without_name_is_rejected(db):
    with pytest.raises(HTTPException) as err:
        client_service.create_client(db, _payload(via_agency=True, agency_name="   "))
    assert "агентство" in err.value.detail.lower()


def test_source_can_be_changed_after_creation(db):
    client = client_service.create_client(db, _payload())
    db.commit()

    client_service.update_source(
        db, client, ClientSourceUpdate(via_agency=True, agency_name="Агентство №2")
    )
    db.commit()
    assert client.via_agency is True
    assert client.agency_name == "Агентство №2"

    # снятие отметки не оставляет названия у клиента, помеченного прямым
    client_service.update_source(db, client, ClientSourceUpdate(via_agency=False))
    db.commit()
    assert client.via_agency is False
    assert client.agency_name is None
    assert client.agency_contact is None


def test_source_endpoint_requires_edit_access(api, make_user, db):
    client = client_service.create_client(db, _payload())
    db.commit()

    viewer = api(make_user(Module.CLIENTS, level=AccessLevel.VIEW))
    denied = viewer.patch(f"/api/clients/{client.id}/source", json={"via_agency": True, "agency_name": "А"})
    assert denied.status_code == 403

    worker = api(make_user(Module.CLIENTS))
    ok = worker.patch(
        f"/api/clients/{client.id}/source",
        json={"via_agency": True, "agency_name": "Агентство №3", "agency_contact": "@agent"},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["agency_name"] == "Агентство №3"

    bad = worker.patch(f"/api/clients/{client.id}/source", json={"via_agency": True, "agency_name": ""})
    assert bad.status_code == 400

    db.expire_all()
    assert db.get(Client, client.id).agency_name == "Агентство №3"
