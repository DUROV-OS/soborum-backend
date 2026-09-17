"""Разрешение администратора на редактирование данных оплаты клиента после
блокировки (0054):

- без `payment_edit_unlocked` блокировка `payment_locked_at` держит всех,
  включая ADMIN — обхода на уровне сервиса нет;
- `PATCH /api/clients/{id}/payment-edit-unlock` доступен только ADMIN, меняет
  `payment_edit_unlocked` независимо от `payment_locked_at`;
- когда `payment_edit_unlocked=True`, `update_payment` проходит и для
  не-администратора.
"""

import pytest
from fastapi import HTTPException

from app.clients import service as client_service
from app.clients.models import Client, ClientStage, PaymentPlan
from app.clients.schemas import ClientCreate, ClientDocumentsUpdate, ClientPaymentUpdate
from app.common.module_access import AccessLevel, Module


def _make_locked_client(db):
    client = client_service.create_client(
        db, ClientCreate(full_name="Иван Тест", phone="+70000000000", email="ivan@example.com")
    )
    client_service.transition_stage(db, client)  # LEAD -> DISCUSSION
    client_service.transition_stage(db, client)  # DISCUSSION -> APPROVAL
    client.contract_file_id = 1
    client.house_project_file_id = 1
    client.contract_appendix_file_id = 1
    client.ar_file_id = 1
    client.kr_file_id = 1
    client_service.update_documents(db, client, ClientDocumentsUpdate(
        order_type="single", final_price=2_000_000, installation_address="г. Тест, ул. Тест, 1",
        payment_plan=PaymentPlan.FULL_PREPAYMENT,
    ))
    client_service.transition_stage(db, client)  # APPROVAL -> PAYMENT
    client_service.update_payment(db, client, ClientPaymentUpdate(is_paid=True))
    client_service.transition_stage(db, client)  # PAYMENT -> POSTPAYMENT
    db.commit()
    assert client.payment_locked_at is not None
    return client


def test_locked_payment_rejects_edit_without_unlock(db):
    client = _make_locked_client(db)
    with pytest.raises(HTTPException) as err:
        client_service.update_payment(db, client, ClientPaymentUpdate(is_paid=False))
    assert err.value.status_code == 400
    assert client.is_paid is True


def test_unlock_allows_edit_of_locked_payment(db):
    client = _make_locked_client(db)
    client_service.set_payment_edit_unlocked(db, client, True)
    db.commit()

    client_service.update_payment(db, client, ClientPaymentUpdate(is_paid=False))
    db.commit()
    assert client.is_paid is False

    client_service.set_payment_edit_unlocked(db, client, False)
    db.commit()
    with pytest.raises(HTTPException) as err:
        client_service.update_payment(db, client, ClientPaymentUpdate(is_paid=True))
    assert err.value.status_code == 400


def test_payment_edit_unlock_endpoint_requires_admin(api, make_user, db):
    client = _make_locked_client(db)
    client_id = client.id

    worker = api(make_user(Module.CLIENTS, level=AccessLevel.EDIT))
    resp = worker.patch(f"/api/clients/{client_id}/payment-edit-unlock", json={"unlocked": True})
    assert resp.status_code == 403
    assert db.get(Client, client_id).payment_edit_unlocked is False


def test_payment_edit_unlock_endpoint_allows_admin(api, make_user, db):
    client = _make_locked_client(db)
    client_id = client.id

    admin = api(make_user(Module.CLIENTS, admin=True))
    resp = admin.patch(f"/api/clients/{client_id}/payment-edit-unlock", json={"unlocked": True})
    assert resp.status_code == 200
    assert resp.json()["payment_edit_unlocked"] is True
    assert db.get(Client, client_id).payment_edit_unlocked is True

    resp = admin.patch(f"/api/clients/{client_id}/payment-edit-unlock", json={"unlocked": False})
    assert resp.status_code == 200
    assert resp.json()["payment_edit_unlocked"] is False
