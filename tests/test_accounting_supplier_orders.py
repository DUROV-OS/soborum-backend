"""Заказы у поставщика (app.accounting.SupplierOrder) — задача 0011-d.

Статус только вперёд без пропуска шага (ordered → in_transit → received),
CRUD, взаиморасчёты/баланс на Supplier. Проводки MoneyMovement и эндпоинт
оплаты здесь не создаются — это 0011-f.
"""

import pytest

from app.common.module_access import Module
from app.warehouse.models import Supplier


@pytest.fixture
def acc_user(make_user):
    # доступ и к бухгалтерии, и к складу: часть проверок читает баланс
    # поставщика через /api/warehouse/suppliers
    return make_user(Module.ACCOUNTING, Module.WAREHOUSE)


def _supplier(db, name="ООО Поставщик"):
    supplier = Supplier(name=name, categories=[], contacts=[])
    db.add(supplier)
    db.commit()
    return supplier


def _order_body(supplier_id, **overrides):
    body = {
        "supplier_id": supplier_id,
        "items": [{"material": "Брус", "category": None, "quantity": 10, "unit_price": 5000}],
    }
    body.update(overrides)
    return body


def test_create_supplier_order_computes_total_cost_and_supplier_balance(db, api, acc_user):
    supplier = _supplier(db)
    api_client = api(acc_user)

    resp = api_client.post("/api/accounting/supplier-orders", json=_order_body(supplier.id))
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "ordered"
    assert data["total_cost"] == 50000

    supplier_resp = api_client.get(f"/api/warehouse/suppliers/{supplier.id}")
    assert supplier_resp.status_code == 200
    supplier_data = supplier_resp.json()
    assert supplier_data["total_ordered"] == 50000
    assert supplier_data["total_paid"] == 0
    assert supplier_data["balance"] == 50000


def test_status_machine_no_skipping_and_received_is_frozen(db, api, acc_user):
    supplier = _supplier(db)
    api_client = api(acc_user)
    order_id = api_client.post(
        "/api/accounting/supplier-orders", json=_order_body(supplier.id)
    ).json()["id"]

    # прыжок через шаг
    assert api_client.post(
        f"/api/accounting/supplier-orders/{order_id}/status", json={"to": "received"}
    ).status_code == 409

    assert api_client.post(
        f"/api/accounting/supplier-orders/{order_id}/status", json={"to": "in_transit"}
    ).status_code == 200
    resp = api_client.post(
        f"/api/accounting/supplier-orders/{order_id}/status", json={"to": "received"}
    )
    assert resp.status_code == 200
    assert resp.json()["received_at"] is not None

    # переход из received куда-либо
    assert api_client.post(
        f"/api/accounting/supplier-orders/{order_id}/status", json={"to": "in_transit"}
    ).status_code == 409

    # правка/удаление принятого заказа
    assert api_client.patch(
        f"/api/accounting/supplier-orders/{order_id}", json={"comment": "поздно"}
    ).status_code == 409
    assert api_client.delete(f"/api/accounting/supplier-orders/{order_id}").status_code == 409


def test_delete_ordered_order_recomputes_balance(db, api, acc_user):
    supplier = _supplier(db)
    api_client = api(acc_user)
    order_id = api_client.post(
        "/api/accounting/supplier-orders", json=_order_body(supplier.id)
    ).json()["id"]

    assert api_client.delete(f"/api/accounting/supplier-orders/{order_id}").status_code == 204

    supplier_data = api_client.get(f"/api/warehouse/suppliers/{supplier.id}").json()
    assert supplier_data["total_ordered"] == 0
    assert supplier_data["balance"] == 0


def test_create_without_items_or_without_price_is_rejected(db, api, acc_user):
    supplier = _supplier(db)
    api_client = api(acc_user)

    assert api_client.post(
        "/api/accounting/supplier-orders", json=_order_body(supplier.id, items=[])
    ).status_code == 422
    assert api_client.post(
        "/api/accounting/supplier-orders",
        json=_order_body(
            supplier.id,
            items=[{"material": "Метизы", "category": None, "quantity": 5, "unit_price": 0}],
        ),
    ).status_code == 422


def test_create_for_missing_supplier_is_404(db, api, acc_user):
    api_client = api(acc_user)
    resp = api_client.post("/api/accounting/supplier-orders", json=_order_body(999999))
    assert resp.status_code == 404
