from datetime import datetime, timezone
from uuid import uuid4
from decimal import Decimal
from app.accounting.models import Counterparty, Organization, MoneyMovement
from app.common.module_access import Module, AccessLevel
from app.sales.models import SalesOrder, SalesShipment, SalesEvent
from app.warehouse.models import (
    WarehouseMaterial,
    Warehouse,
    MaterialCategory,
    StockMovement,
)
from app.warehouse.reservation_models import StockReservation


def setup(db, api, make_user, quantity=10, fractional=False):
    owner = make_user(admin=True)
    cp = Counterparty(name="Тестовый покупатель", is_active=True)
    m = WarehouseMaterial(
        title="Брус",
        code="SALES-1",
        warehouse=Warehouse.TECHNOLOGY,
        category=MaterialCategory.NONE,
        unit="м" if fractional else "шт",
        is_fractional=fractional,
        quantity_in_stock=quantity,
        purchase_price=40,
        threshold=0,
    )
    db.add_all([cp, m])
    db.commit()
    org = db.query(Organization).first()
    return api(owner), owner, cp, org, m


def order(client, cp, org, m, quantity="6", lines=None):
    body = {
        "request_id": str(uuid4()),
        "counterparty_id": cp.id,
        "organization_id": org.id,
        "lines": lines
        or [{"material_id": m.id, "quantity": quantity, "unit_price": "100.12"}],
        "expected_at": "2026-11-30",
        "address": "Площадка",
        "note": "Тестовый заказ",
    }
    response = client.post("/api/sales/orders", json=body)
    assert response.status_code == 201, response.text
    row = response.json()
    confirmed = client.post(
        f"/api/sales/orders/{row['id']}/confirm", json={"version": row["version"]}
    )
    assert confirmed.status_code == 200, confirmed.text
    return confirmed.json(), body


def shipment(client, row, quantity="2", lines=None):
    latest = client.get(f"/api/sales/orders/{row['id']}").json()
    response = client.post(
        "/api/sales/shipments",
        json={
            "request_id": str(uuid4()),
            "order_id": row["id"],
            "order_version": latest["version"],
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "received_by": "Получатель",
            "lines": lines or [{"order_line": 0, "quantity": quantity}],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def post(client, row, **changes):
    body = {
        "request_id": str(uuid4()),
        "version": row["version"],
        "order_version": row["order_version"],
        **changes,
    }
    response = client.post(f"/api/sales/shipments/{row['id']}/post", json=body)
    return response, body


def reserve(client, order_row, m, quantity, index=0):
    response = client.post(
        "/api/warehouse/reservations",
        json={
            "request_id": str(uuid4()),
            "material_id": m.id,
            "quantity": str(quantity),
            "sales_order_id": order_row["id"],
            "sales_line": index,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_partial_shipments_consume_reserves_preserve_money_and_retry_once(
    db, api, make_user
):
    client, owner, cp, org, m = setup(db, api, make_user)
    row, create_body = order(client, cp, org, m)
    assert row["total"] == 600.72
    assert client.post("/api/sales/orders", json=create_body).json()["id"] == row["id"]
    hold = reserve(client, row, m, 6)
    draft = shipment(client, row, "2")
    assert db.get(WarehouseMaterial, m.id).quantity_in_stock == 10
    result, body = post(client, draft)
    assert result.status_code == 200, result.text
    posted = result.json()
    assert posted["status"] == "posted" and posted["operation_id"]
    assert db.get(WarehouseMaterial, m.id).quantity_in_stock == 8
    assert (
        client.get(f"/api/warehouse/reservations/{hold['id']}").json()[
            "remaining_quantity"
        ]
        == 4
    )
    again = client.post(f"/api/sales/shipments/{draft['id']}/post", json=body)
    assert again.status_code == 200 and again.json()["id"] == posted["id"]
    assert db.query(StockMovement).count() == 1
    assert db.query(MoneyMovement).count() == 0
    current = client.get(f"/api/sales/orders/{row['id']}").json()
    assert current["fulfillment"] == "partial"
    assert current["lines"][0]["remaining_quantity"] == 4
    draft2 = shipment(client, row, "4")
    assert post(client, draft2)[0].status_code == 200
    assert (
        client.get(f"/api/sales/orders/{row['id']}").json()["fulfillment"] == "complete"
    )
    assert (
        client.get(f"/api/warehouse/reservations/{hold['id']}").json()["status"]
        == "issued"
    )
    assert db.query(StockMovement).count() == 2


def test_other_reservations_and_another_line_of_same_material_stay_protected(
    db, api, make_user
):
    client, owner, cp, org, m = setup(db, api, make_user, quantity=6)
    row, _ = order(
        client,
        cp,
        org,
        m,
        lines=[
            {"material_id": m.id, "quantity": "5", "unit_price": "100"}
            for _ in range(2)
        ],
    )
    hold = reserve(client, row, m, 5, index=0)
    draft = shipment(
        client,
        row,
        lines=[{"order_line": 0, "quantity": "1"}, {"order_line": 1, "quantity": "5"}],
    )
    assert post(client, draft)[0].status_code == 409
    assert db.get(WarehouseMaterial, m.id).quantity_in_stock == 6
    assert db.query(StockMovement).count() == 0
    assert (
        client.get(f"/api/warehouse/reservations/{hold['id']}").json()[
            "remaining_quantity"
        ]
        == 5
    )
    good = shipment(client, row, "2")
    assert post(client, good)[0].status_code == 200
    assert (
        client.get(f"/api/warehouse/reservations/{hold['id']}").json()[
            "remaining_quantity"
        ]
        == 3
    )
    another, _ = order(client, cp, org, m, "2")
    denied = shipment(client, another, "2")
    assert post(client, denied)[0].status_code == 409
    assert db.get(WarehouseMaterial, m.id).quantity_in_stock == 4


def test_two_drafts_cannot_overfulfil_order_and_stale_version_rejected(
    db, api, make_user
):
    client, owner, cp, org, m = setup(db, api, make_user)
    row, _ = order(client, cp, org, m, "3")
    a, b = shipment(client, row, "2"), shipment(client, row, "2")
    assert post(client, a)[0].status_code == 200
    assert post(client, b)[0].status_code == 409
    b = client.get(f"/api/sales/shipments/{b['id']}").json()
    assert (
        post(client, b)[0].status_code == 409
    )  # quantity, even after updating version
    assert db.get(WarehouseMaterial, m.id).quantity_in_stock == 8


def test_cancellation_restores_stock_and_order_but_does_not_recreate_hold(
    db, api, make_user
):
    client, owner, cp, org, m = setup(db, api, make_user)
    row, _ = order(client, cp, org, m)
    hold = reserve(client, row, m, 3)
    draft = shipment(client, row, "3")
    result, _ = post(client, draft)
    posted = result.json()
    # The current average price may have changed since the sale; restore the
    # value actually written off, rather than assigning today's price to it.
    db.get(WarehouseMaterial, m.id).purchase_price = 100
    db.commit()
    body = {
        "request_id": str(uuid4()),
        "version": posted["version"],
        "note": "Ошибка оформления",
    }
    response = client.post(f"/api/sales/shipments/{posted['id']}/cancel", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["cancel_operation_id"] != posted["operation_id"]
    assert db.get(WarehouseMaterial, m.id).quantity_in_stock == 10
    assert db.get(WarehouseMaterial, m.id).purchase_price == 82
    assert (
        client.post(
            f"/api/sales/shipments/{posted['id']}/cancel", json=body
        ).status_code
        == 200
    )
    assert db.query(StockMovement).count() == 2
    assert (
        client.get(f"/api/warehouse/reservations/{hold['id']}").json()[
            "remaining_quantity"
        ]
        == 0
    )
    assert (
        client.get(f"/api/sales/orders/{row['id']}").json()["lines"][0][
            "remaining_quantity"
        ]
        == 6
    )
    assert db.query(MoneyMovement).count() == 0
    # Returned goods can be promised again through a new, audited reservation.
    reserve(client, row, m, 6)


def test_order_archive_edit_and_cancel_guards_preserve_audit(db, api, make_user):
    client, owner, cp, org, m = setup(db, api, make_user)
    row, _ = order(client, cp, org, m)
    hold = reserve(client, row, m, 1)
    assert (
        client.post(
            f"/api/sales/orders/{row['id']}/archive",
            json={"version": row["version"], "archived": True},
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"/api/sales/orders/{row['id']}/cancel",
            json={
                "request_id": str(uuid4()),
                "version": row["version"],
                "note": "Отмена",
            },
        ).status_code
        == 409
    )
    assert (
        client.patch(
            f"/api/sales/orders/{row['id']}",
            json={
                "version": row["version"],
                "lines": [{"material_id": m.id, "quantity": "5", "unit_price": "50"}],
            },
        ).status_code
        == 409
    )
    assert (
        client.patch(
            f"/api/warehouse/materials/{m.id}", json={"unit": "компл"}
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"/api/warehouse/reservations/{hold['id']}/issue",
            json={
                "request_id": str(uuid4()),
                "version": hold["version"],
                "quantity": "1",
                "note": "Выдача",
                "received_by": "Цех",
            },
        ).status_code
        == 409
    )
    released = client.post(
        f"/api/warehouse/reservations/{hold['id']}/release",
        json={
            "request_id": str(uuid4()),
            "version": hold["version"],
            "quantity": "1",
            "note": "Снятие",
        },
    )
    assert released.status_code == 200
    cancelled = client.post(
        f"/api/sales/orders/{row['id']}/cancel",
        json={
            "request_id": str(uuid4()),
            "version": row["version"],
            "note": "Отмена заказа",
        },
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["events"][-1]["kind"] == "order_cancelled"
    archived = client.post(
        f"/api/sales/orders/{row['id']}/archive",
        json={"version": cancelled.json()["version"], "archived": True},
    )
    assert archived.status_code == 200
    assert client.get("/api/sales/orders?archive=archived").json()["total"] == 1


def test_services_and_fractional_amounts_do_not_create_fake_stock(db, api, make_user):
    client, owner, cp, org, m = setup(db, api, make_user, fractional=True)
    row, _ = order(
        client,
        cp,
        org,
        m,
        lines=[
            {"material_id": m.id, "quantity": "0.3", "unit_price": "1.15"},
            {
                "stock_tracked": False,
                "title": "Доставка",
                "unit": "усл",
                "quantity": "1",
                "unit_price": "1000",
            },
        ],
    )
    assert row["total"] == 1000.35
    draft = shipment(
        client,
        row,
        lines=[
            {"order_line": 0, "quantity": "0.3"},
            {"order_line": 1, "quantity": "1"},
        ],
    )
    assert post(client, draft)[0].status_code == 200
    assert Decimal(str(db.get(WarehouseMaterial, m.id).quantity_in_stock)) == Decimal(
        "9.7"
    )
    assert db.query(StockMovement).count() == 1
    service_order, _ = order(
        client,
        cp,
        org,
        m,
        lines=[
            {
                "stock_tracked": False,
                "title": "Монтаж",
                "unit": "усл",
                "quantity": "1",
                "unit_price": "1000",
            }
        ],
    )
    draft = shipment(client, service_order, "1")
    response, _ = post(client, draft)
    assert response.status_code == 200 and response.json()["operation_id"] is None
    assert db.query(StockMovement).count() == 1


def test_permissions_validation_and_duplicate_buyer(db, api, make_user):
    client, owner, cp, org, m = setup(db, api, make_user)
    row, body = order(client, cp, org, m)
    viewer = make_user(Module.CLIENTS, level=AccessLevel.VIEW)
    assert api(viewer).get("/api/sales/orders").status_code == 200
    assert api(viewer).post("/api/sales/orders", json=body).status_code == 403
    stock_only = make_user(Module.WAREHOUSE, level=AccessLevel.FULL)
    assert api(stock_only).get("/api/sales/orders").status_code == 403
    client = api(owner)
    body["request_id"] = str(uuid4())
    body["lines"][0]["quantity"] = "0.5"
    assert client.post("/api/sales/orders", json=body).status_code == 422
    body["lines"][0]["quantity"] = "NaN"
    assert client.post("/api/sales/orders", json=body).status_code == 422
    assert (
        client.patch(
            f"/api/sales/orders/{row['id']}",
            json={"version": row["version"], "lines": None},
        ).status_code
        == 422
    )
    buyer = client.post(
        "/api/sales/buyers", json={"name": "Новый покупатель", "inn": "1234567890"}
    )
    assert buyer.status_code == 201
    again = client.post(
        "/api/sales/buyers", json={"name": "Второе название", "inn": "1234567890"}
    )
    assert again.json()["id"] == buyer.json()["id"]


def test_manager_can_cancel_draft_but_cannot_reverse_posted_stock(db, api, make_user):
    client, owner, cp, org, m = setup(db, api, make_user)
    row, _ = order(client, cp, org, m)
    draft = shipment(client, row, "2")
    manager = make_user(Module.CLIENTS, level=AccessLevel.EDIT)
    response = api(manager).post(
        f"/api/sales/shipments/{draft['id']}/cancel",
        json={
            "request_id": str(uuid4()),
            "version": draft["version"],
            "note": "Черновик больше не нужен",
        },
    )
    assert response.status_code == 200
    assert db.query(StockMovement).count() == 0
    client = api(owner)
    draft = shipment(client, row, "2")
    posted = post(client, draft)[0].json()
    response = api(manager).post(
        f"/api/sales/shipments/{posted['id']}/cancel",
        json={
            "request_id": str(uuid4()),
            "version": posted["version"],
            "note": "Недостаточно прав",
        },
    )
    assert response.status_code == 403
    assert db.get(WarehouseMaterial, m.id).quantity_in_stock == 8


def test_sales_import_preserves_history_links_and_never_replays(db, api, make_user):
    from app.moysklad.models import SourceRecord, SourceLink
    from app.sales.importer import import_native

    client, owner, cp, org, m = setup(db, api, make_user)

    def src(typ, sid, payload, archived=False):
        s = SourceRecord(
            entity_type=typ,
            source_id=sid,
            title=sid,
            payload=payload,
            archived=archived,
            search_text="",
        )
        db.add(s)
        db.flush()
        return s

    p = {
        "id": "position1",
        "assortment": {"id": "prod1", "meta": {"type": "product"}},
        "quantity": 5,
        "shipped": 2,
        "price": 10000,
    }
    base = {
        "agent": {"id": "cp1"},
        "organization": {"id": "org1"},
        "store": {"id": "store1"},
        "positions": {"rows": [p]},
        "applicable": True,
        "sum": 50000,
        "payedSum": 10000,
        "moment": "2026-10-01 12:00:00",
    }
    source = src("customerorder", "order1", base, True)
    src(
        "invoiceout",
        "invoice1",
        {"customerOrder": {"id": "order1"}, "demands": [{"id": "demand1"}]},
    )
    src(
        "demand",
        "demand1",
        {
            **base,
            "positions": {"rows": [{**p, "quantity": 2}]},
            "invoicesOut": [{"id": "invoice1"}],
        },
        True,
    )
    for key, table, id in [
        ("counterparty/cp1", "counterparties", cp.id),
        ("organization/org1", "organizations", org.id),
        ("stock/prod1/store1", "warehouse_materials", m.id),
    ]:
        db.add(SourceLink(source_key=key, target_type=table, target_id=id))
    db.add(
        StockReservation(
            source_order_id=source.id,
            source_line=0,
            material_id=m.id,
            purpose="Старый резерв",
            quantity=1,
            consumed_quantity=1,
            released_quantity=0,
            unit=m.unit,
            actor_id=owner.id,
            request_id=str(uuid4()),
            parameters={},
            version=1,
            archived=False,
        )
    )
    db.commit()
    before = (
        m.quantity_in_stock,
        db.query(StockMovement).count(),
        db.query(MoneyMovement).count(),
    )
    result = import_native(db, owner.id)
    db.commit()
    assert result == {
        "orders": 1,
        "shipments": 1,
        "converted_reservations": 1,
        "unlinked_shipments": 0,
    }
    row = db.query(SalesOrder).one()
    document = db.query(SalesShipment).one()
    assert document.order_id == row.id and document.historical and document.archived
    assert row.archived and row.opening_paid == 100
    assert (
        client.get(f"/api/sales/orders/{row.id}").json()["lines"][0]["shipped_quantity"]
        == 3
    )
    assert db.query(StockReservation).one().sales_order_id == row.id
    assert (
        db.get(WarehouseMaterial, m.id).quantity_in_stock,
        db.query(StockMovement).count(),
        db.query(MoneyMovement).count(),
    ) == before
    assert import_native(db, owner.id) == {
        "orders": 0,
        "shipments": 0,
        "converted_reservations": 0,
        "unlinked_shipments": 0,
    }
    assert (
        client.post(
            f"/api/sales/shipments/{document.id}/cancel",
            json={
                "request_id": str(uuid4()),
                "version": 1,
                "note": "Нельзя повторять историю",
            },
        ).status_code
        == 409
    )
