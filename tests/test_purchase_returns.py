from datetime import datetime, timezone, timedelta
from uuid import uuid4
from decimal import Decimal
from app.common.module_access import Module, AccessLevel
from app.warehouse.models import (
    Supplier,
    WarehouseMaterial,
    Warehouse,
    MaterialCategory,
    StockMovement,
)
from app.accounting.models import MoneyMovement, MoneyMovementStatus, BankAccount
from app.accounting import service as accounting
from app.accounting.schemas import MoneyMovementUpdate
from app.purchasing.models import PurchaseReturn


def test_historical_receipt_prevents_order_repricing_even_when_source_shipped_flag_is_zero(
    db, api, make_user
):
    from app.accounting.models import SupplierOrder, SupplierOrderStatus
    from app.accounting.schemas import SupplierOrderUpdate, SupplierOrderItem
    from fastapi import HTTPException
    import pytest

    client, owner, supplier, material, order, receipt = setup(db, api, make_user)
    source_order = db.get(SupplierOrder, order["id"])
    source_order.status = SupplierOrderStatus.ORDERED
    source_order.items = [
        {**line, "received_quantity": 0} for line in source_order.items
    ]
    db.commit()
    with pytest.raises(HTTPException):
        accounting.update_supplier_order(
            db,
            source_order,
            SupplierOrderUpdate(
                items=[SupplierOrderItem(material="Брус", quantity=10, unit_price=1)]
            ),
        )


def setup(db, api, make_user, paid=0):
    owner = make_user(admin=True)
    client = api(owner)
    supplier = Supplier(name="Поставщик возвратов", categories=[], contacts=[])
    material = WarehouseMaterial(
        title="Брус",
        code="RETURN-TEST",
        warehouse=Warehouse.TECHNOLOGY,
        unit="м³",
        is_fractional=True,
        category=MaterialCategory.NONE,
        quantity_in_stock=5,
        purchase_price=10,
    )
    db.add_all([supplier, material])
    db.commit()
    order = client.post(
        "/api/purchasing/orders",
        json={
            "supplier_id": supplier.id,
            "items": [
                {
                    "material": "Брус",
                    "quantity": 10,
                    "unit_price": 20,
                    "warehouse_material_id": material.id,
                    "unit": "м³",
                }
            ],
        },
    ).json()
    when = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    receipt = client.post(
        "/api/purchasing/receipts",
        json={
            "request_id": str(uuid4()),
            "order_id": order["id"],
            "version": order["version"],
            "occurred_at": when,
            "lines": [
                {
                    "order_line": 0,
                    "material_id": material.id,
                    "quantity": "10",
                    "unit_price": "20",
                }
            ],
        },
    ).json()
    if paid:
        from app.accounting.models import SupplierOrder

        db.get(SupplierOrder, order["id"]).opening_paid = paid
        db.commit()
    return client, owner, supplier, material, order, receipt


def body(receipt, qty="3"):
    return {
        "request_id": str(uuid4()),
        "receipt_id": receipt["id"],
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "note": "Брак при приёмке",
        "lines": [{"receipt_line": 0, "quantity": qty}],
    }


def test_partial_return_credit_stock_retry_and_archive_preserve_accounting(
    db, api, make_user
):
    client, owner, supplier, material, order, receipt = setup(db, api, make_user)
    request = body(receipt)
    response = client.post("/api/purchasing/returns", json=request)
    assert response.status_code == 201, response.text
    row = response.json()
    assert row["amount"] == 60
    assert (
        client.post("/api/purchasing/returns", json=request).json()["id"] == row["id"]
    )
    assert (
        client.post(
            "/api/purchasing/returns", json={**request, "note": "Другие данные"}
        ).status_code
        == 409
    )
    db.refresh(material)
    assert material.quantity_in_stock == 12
    assert db.query(StockMovement).count() == 2
    assert db.query(MoneyMovement).count() == 0
    updated = client.get(f"/api/purchasing/orders/{order['id']}").json()
    assert (
        updated["net_total"] == 140
        and updated["return_credit"] == 60
        and updated["available_payment"] == 140
    )
    summary = client.get(f"/api/purchasing/receipts/{receipt['id']}").json()
    assert summary["lines"][0]["returnable_quantity"] == 7
    assert (
        client.post("/api/purchasing/returns", json=body(receipt, "8")).status_code
        == 409
    )
    archived = client.post(
        f"/api/purchasing/returns/{row['id']}/archive",
        json={"version": 1, "archived": True},
    )
    assert archived.status_code == 200
    assert client.get("/api/purchasing/returns?archive=archived").json()["total"] == 1
    assert (
        client.get(f"/api/purchasing/receipts/{receipt['id']}").json()["lines"][0][
            "returned_quantity"
        ]
        == 3
    )
    assert (
        client.get(f"/api/purchasing/orders/{order['id']}").json()["net_total"] == 140
    )


def test_refund_approval_limits_edits_cancellation_and_repayment(db, api, make_user):
    client, owner, supplier, material, order, receipt = setup(
        db, api, make_user, paid=200
    )
    row = client.post("/api/purchasing/returns", json=body(receipt)).json()
    account = db.query(BankAccount).first()
    request = {"request_id": str(uuid4()), "amount": "40", "account_id": account.id}
    result = client.post(f"/api/purchasing/returns/{row['id']}/refunds", json=request)
    assert result.status_code == 201, result.text
    movement = result.json()
    assert (
        movement["purchase_return_id"] == row["id"]
        and movement["affects_profit"] is False
        and movement["status"] == "draft"
    )
    assert (
        client.post(
            f"/api/purchasing/returns/{row['id']}/refunds", json=request
        ).json()["id"]
        == movement["id"]
    )
    assert (
        client.post(
            f"/api/purchasing/returns/{row['id']}/refunds",
            json={**request, "request_id": str(uuid4()), "amount": "21"},
        ).status_code
        == 409
    )
    mm = db.get(MoneyMovement, movement["id"])
    import pytest
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        accounting.update_money_movement(db, mm, MoneyMovementUpdate(amount=61))
    db.rollback()
    with pytest.raises(HTTPException):
        accounting.update_money_movement(
            db, mm, MoneyMovementUpdate(purchase_return_id=None)
        )
    db.rollback()
    accounting.change_status(db, mm, MoneyMovementStatus.APPROVED)
    accounting.change_status(db, mm, MoneyMovementStatus.POSTED)
    updated = client.get(f"/api/purchasing/orders/{order['id']}").json()
    assert (
        updated["paid"] == 160
        and updated["overpaid"] == 20
        and updated["refund_received"] == 40
    )
    db.refresh(supplier)
    assert supplier.total_ordered == 140 and supplier.total_paid == 160
    accounting.change_status(
        db, mm, MoneyMovementStatus.CANCELLED, reason="Банк отклонил перевод"
    )
    assert (
        client.get(f"/api/purchasing/returns/{row['id']}").json()["refund_available"]
        == 60
    )
    assert client.get(f"/api/purchasing/orders/{order['id']}").json()["paid"] == 200


def test_pending_payment_blocks_an_excess_return_and_payment_cap_uses_credit(
    db, api, make_user
):
    client, owner, supplier, material, order, receipt = setup(db, api, make_user)
    account = db.query(BankAccount).first()
    payment = client.post(
        f"/api/purchasing/orders/{order['id']}/payments",
        json={"request_id": str(uuid4()), "amount": "180", "account_id": account.id},
    ).json()
    assert client.post("/api/purchasing/returns", json=body(receipt)).status_code == 409
    db.rollback()
    accounting.change_status(
        db,
        db.get(MoneyMovement, payment["id"]),
        MoneyMovementStatus.CANCELLED,
        reason="Корректировка закупки",
    )
    assert client.post("/api/purchasing/returns", json=body(receipt)).status_code == 201
    assert (
        client.post(
            f"/api/purchasing/orders/{order['id']}/payments",
            json={
                "request_id": str(uuid4()),
                "amount": "141",
                "account_id": account.id,
            },
        ).status_code
        == 409
    )


def test_stock_quantities_service_lines_and_permissions(db, api, make_user):
    client, owner, supplier, material, order, receipt = setup(db, api, make_user)
    material.quantity_in_stock = 1
    db.commit()
    assert (
        client.post("/api/purchasing/returns", json=body(receipt, "2")).status_code
        == 409
    )
    viewer = make_user(Module.ACCOUNTING, level=AccessLevel.VIEW)
    assert api(viewer).get("/api/purchasing/returns").status_code == 200
    assert (
        api(viewer).post("/api/purchasing/returns", json=body(receipt, "1")).status_code
        == 403
    )
    editor = make_user(Module.ACCOUNTING, level=AccessLevel.EDIT)
    assert (
        api(editor).post("/api/purchasing/returns", json=body(receipt, "1")).status_code
        == 403
    )
    client = api(owner)
    for quantity in ("-1", "NaN", "Infinity", "0.0000001"):
        assert (
            client.post(
                "/api/purchasing/returns", json=body(receipt, quantity)
            ).status_code
            == 422
        )
    request = body(receipt, "1")
    request["lines"][0]["material_id"] = material.id + 1000
    assert client.post("/api/purchasing/returns", json=request).status_code == 422
    assert db.query(PurchaseReturn).count() == 0


def test_cancellation_requires_refund_cleanup_and_reverses_stock_once(
    db, api, make_user
):
    client, owner, supplier, material, order, receipt = setup(
        db, api, make_user, paid=200
    )
    row = client.post("/api/purchasing/returns", json=body(receipt)).json()
    account = db.query(BankAccount).first()
    refund = client.post(
        f"/api/purchasing/returns/{row['id']}/refunds",
        json={"request_id": str(uuid4()), "amount": "10", "account_id": account.id},
    ).json()
    request = {
        "request_id": str(uuid4()),
        "version": 1,
        "note": "Возврат оформили ошибочно",
    }
    assert (
        client.post(
            f"/api/purchasing/returns/{row['id']}/cancel", json=request
        ).status_code
        == 409
    )
    db.rollback()
    accounting.change_status(
        db,
        db.get(MoneyMovement, refund["id"]),
        MoneyMovementStatus.CANCELLED,
        reason="Отмена возврата",
    )
    db.refresh(material)
    cost = Decimal(str(material.purchase_price))
    material.quantity_in_stock = 14
    material.purchase_price = 30
    db.commit()
    response = client.post(f"/api/purchasing/returns/{row['id']}/cancel", json=request)
    assert response.status_code == 200, response.text
    assert response.json()["cancelled"]
    assert (
        client.post(
            f"/api/purchasing/returns/{row['id']}/cancel", json=request
        ).status_code
        == 200
    )
    db.refresh(material)
    assert material.quantity_in_stock == 17
    assert abs(
        Decimal(str(material.purchase_price)) - (Decimal(420) + 3 * cost) / 17
    ) < Decimal("0.00000001")
    assert db.query(StockMovement).count() == 3
    assert (
        client.get(f"/api/purchasing/orders/{order['id']}").json()["net_total"] == 200
    )
    assert (
        client.get(f"/api/purchasing/receipts/{receipt['id']}").json()["lines"][0][
            "returnable_quantity"
        ]
        == 10
    )
    assert (
        client.post(
            f"/api/purchasing/returns/{row['id']}/refunds",
            json={"request_id": str(uuid4()), "amount": "10", "account_id": account.id},
        ).status_code
        == 409
    )
