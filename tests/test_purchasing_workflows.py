from uuid import uuid4
from decimal import Decimal
import pytest
from app.warehouse.models import Supplier, WarehouseMaterial, Warehouse, MaterialCategory, StockMovement
from app.accounting.models import SupplierOrder, MoneyMovement
from app.purchasing.models import PurchaseReceipt
from app.moysklad.models import SourceRecord, SourceLink
from app.common.module_access import Module, AccessLevel


def setup(db, api, make_user):
    owner = make_user(admin=True)
    supplier = Supplier(name="Поставщик", contacts=[], categories=[])
    material = WarehouseMaterial(
        title="Брус",
        code="PUR-TEST",
        warehouse=Warehouse.TECHNOLOGY,
        category=MaterialCategory.NONE,
        unit="м³",
        is_fractional=True,
        quantity_in_stock=10,
        purchase_price=10,
    )
    db.add_all([supplier, material])
    db.commit()
    client = api(owner)
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
    assert "id" in order, order
    return client, owner, supplier, material, order


def receipt(order, material, quantity="3", **extra):
    return {
        "request_id": str(uuid4()),
        "order_id": order["id"],
        "version": order["version"],
        "occurred_at": "2026-10-29T10:00:00+03:00",
        "lines": [{"order_line": 0, "material_id": material.id, "quantity": quantity, "unit_price": "20"}],
        **extra,
    }


def test_partial_receipts_stock_cost_retry_and_completion(db, api, make_user):
    client, owner, supplier, material, order = setup(db, api, make_user)
    body = receipt(order, material)
    first = client.post("/api/purchasing/receipts", json=body)
    assert first.status_code == 201, first.text
    assert client.post("/api/purchasing/receipts", json=body).json()["id"] == first.json()["id"]
    db.refresh(material)
    assert material.quantity_in_stock == 13
    assert Decimal(str(material.purchase_price)) == Decimal("12.30769231")
    assert db.query(StockMovement).count() == 1
    order = client.get(f"/api/purchasing/orders/{order['id']}").json()
    assert order["status"] == "in_transit" and order["items"][0]["received_quantity"] == 3
    assert client.post("/api/purchasing/receipts", json=receipt(order, material, "8")).status_code == 409
    assert client.post("/api/purchasing/receipts", json=receipt(order, material, "7")).status_code == 201
    order = client.get(f"/api/purchasing/orders/{order['id']}").json()
    assert order["status"] == "received"
    db.refresh(material)
    assert material.quantity_in_stock == 20
    assert db.query(PurchaseReceipt).count() == 2


def test_rejected_receipt_is_atomic_and_versioned(db, api, make_user):
    client, owner, supplier, material, order = setup(db, api, make_user)
    body = receipt(order, material)
    body["lines"].append({"order_line": 0, "material_id": material.id, "quantity": "1", "unit_price": "20"})
    assert client.post("/api/purchasing/receipts", json=body).status_code == 422
    assert db.query(PurchaseReceipt).count() == 0
    db.refresh(material)
    assert material.quantity_in_stock == 10
    body = receipt(order, material)
    body["version"] = 99
    assert client.post("/api/purchasing/receipts", json=body).status_code == 409
    body = receipt(order, material)
    body["occurred_at"] = "2026-10-29T10:00:00"
    assert client.post("/api/purchasing/receipts", json=body).status_code == 422
    material.is_archived = True
    db.commit()
    assert client.post("/api/purchasing/receipts", json=receipt(order, material)).status_code == 422
    assert db.query(StockMovement).count() == 0


def test_partial_payments_pending_caps_edits_and_retry(db, api, make_user, default_account):
    client, owner, supplier, material, order = setup(db, api, make_user)
    body = {"request_id": str(uuid4()), "amount": "70.00", "account_id": default_account.id}
    first = client.post(f"/api/purchasing/orders/{order['id']}/payments", json=body)
    assert first.status_code == 201, first.text
    assert (
        client.post(f"/api/purchasing/orders/{order['id']}/payments", json=body).json()["id"]
        == first.json()["id"]
    )
    second = client.post(
        f"/api/purchasing/orders/{order['id']}/payments",
        json={**body, "request_id": str(uuid4()), "amount": "130.00"},
    )
    assert second.status_code == 201, second.text
    assert db.query(MoneyMovement).count() == 2
    assert (
        client.post(
            f"/api/purchasing/orders/{order['id']}/payments",
            json={**body, "request_id": str(uuid4()), "amount": "0.01"},
        ).status_code
        == 409
    )
    assert (
        client.patch(f"/api/accounting/money-movements/{first.json()['id']}", json={"amount": 80}).status_code
        == 409
    )
    client.post(
        f"/api/accounting/money-movements/{second.json()['id']}/status",
        json={"to": "cancelled", "reason": "Отмена второго платежа"},
    )
    row = client.get(f"/api/purchasing/orders/{order['id']}").json()
    assert row["available_payment"] == 130 and row["pending_payment"] == 70 and row["paid"] == 0


def test_purchasing_permissions_and_archives(db, api, make_user):
    client, owner, supplier, material, order = setup(db, api, make_user)
    warehouse_only = api(make_user(Module.WAREHOUSE))
    assert warehouse_only.get("/api/purchasing/orders").status_code == 403
    viewer = api(make_user(Module.ACCOUNTING, Module.WAREHOUSE, level=AccessLevel.VIEW))
    assert viewer.get("/api/purchasing/orders").status_code == 200
    assert viewer.post("/api/purchasing/receipts", json=receipt(order, material)).status_code == 403
    client = api(owner)
    assert (
        client.post(
            f"/api/purchasing/orders/{order['id']}/archive", json={"version": 1, "archived": True}
        ).status_code
        == 200
    )
    assert client.get("/api/purchasing/orders?archive=archived").json()["total"] == 1
    assert client.get("/api/purchasing/orders").json()["total"] == 0
    assert client.post("/api/purchasing/receipts", json=receipt(order, material)).status_code == 409


def test_source_documents_import_does_not_replay_stock_or_payments(db, make_user):
    from app.purchasing.importer import import_native

    owner = make_user(admin=True)
    supplier = Supplier(name="Поставщик", contacts=[], categories=[])
    material = WarehouseMaterial(
        title="Материал",
        code="SRC",
        warehouse=Warehouse.TECHNOLOGY,
        category=MaterialCategory.NONE,
        unit="шт",
        quantity_in_stock=100,
        purchase_price=20,
    )
    db.add_all([supplier, material])
    db.flush()
    db.add_all(
        [
            SourceLink(source_key="counterparty/agent", target_type="suppliers", target_id=supplier.id),
            SourceLink(
                source_key="stock/product/store", target_type="warehouse_materials", target_id=material.id
            ),
        ]
    )
    common = {
        "agent": {"id": "agent"},
        "store": {"id": "store"},
        "moment": "2026-10-28 10:00:00",
        "applicable": True,
    }
    for typ, sid, payload in [
        (
            "purchaseorder",
            "order",
            {
                **common,
                "sum": 100000,
                "payedSum": 30000,
                "positions": {
                    "rows": [
                        {
                            "assortment": {"id": "product", "name": "Материал"},
                            "quantity": 10,
                            "shipped": 2,
                            "price": 10000,
                        }
                    ]
                },
            },
        ),
        (
            "supply",
            "receipt",
            {
                **common,
                "purchaseOrder": {"id": "order"},
                "positions": {
                    "rows": [
                        {"assortment": {"id": "product", "name": "Материал"}, "quantity": 2, "price": 10000}
                    ]
                },
            },
        ),
    ]:
        db.add(
            SourceRecord(
                entity_type=typ, source_id=sid, title="1", archived=False, search_text="", payload=payload
            )
        )
    db.flush()
    assert import_native(db, owner.id) == {"orders": 1, "receipts": 1, "suppliers": 0}
    db.commit()
    db.refresh(material)
    assert material.quantity_in_stock == 100
    assert db.query(StockMovement).count() == 0 and db.query(MoneyMovement).count() == 0
    order = db.query(SupplierOrder).one()
    order.comment = "Правка сотрудника"
    db.commit()
    assert import_native(db, owner.id) == {"orders": 0, "receipts": 0, "suppliers": 0}
    assert order.comment == "Правка сотрудника" and order.opening_paid == 300
    assert db.query(PurchaseReceipt).one().order_id == order.id


def test_automatic_review_is_read_only_and_never_infers_trust_or_price(db, api, make_user):
    client, owner, supplier, material, order = setup(db, api, make_user)
    before = client.get(f"/api/purchasing/orders/{order['id']}").json()
    result = client.get(f"/api/purchasing/orders/{order['id']}/automatic-review")
    assert result.status_code == 200
    report = result.json()
    assert report['decision'] == 'manual_review'
    assert not report['auto_approval_enabled'] and not report['auto_payment_enabled']
    codes = {item['code'] for item in report['reasons']}
    assert {'supplier_verification_missing', 'sale_link_missing', 'techcard_link_missing',
            'price_evidence_missing', 'payment_controls_missing'} <= codes
    assert client.get(f"/api/purchasing/orders/{order['id']}").json() == before
    assert api(make_user()).get(f"/api/purchasing/orders/{order['id']}/automatic-review").status_code == 403
    assert api(owner).get('/api/purchasing/orders/999999/automatic-review').status_code == 404
