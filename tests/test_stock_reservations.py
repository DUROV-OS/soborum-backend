from uuid import uuid4
from datetime import datetime, timezone
from decimal import Decimal
from app.warehouse.models import (
    WarehouseMaterial,
    Warehouse,
    MaterialCategory,
    StockMovement,
    WarehouseOperation,
)
from app.warehouse.reservation_models import StockReservation, ReservationEvent
from app.common.module_access import Module, AccessLevel
from app.production.models import Production, ProductionBlock, BlockMaterial
from app.cycle.models import Cycle, CycleStatus


def setup(db, api, make_user, quantity=10, fractional=False):
    owner = make_user(admin=True)
    m = WarehouseMaterial(
        title="Брус",
        code="RES-TEST",
        warehouse=Warehouse.TECHNOLOGY,
        category=MaterialCategory.NONE,
        unit="шт",
        is_fractional=fractional,
        quantity_in_stock=quantity,
        purchase_price=10,
    )
    db.add(m)
    db.commit()
    return api(owner), owner, m


def test_recipes_and_new_source_snapshots_cannot_bypass_holds(db, api, make_user):
    from app.moysklad.recipes import (
        Recipe,
        Manufacturing,
        preview,
        manufacture,
        ManufactureRun,
    )
    from app.moysklad.importer import import_export
    from fastapi import HTTPException
    import pytest

    client, owner, m = setup(db, api, make_user)
    reserve(client, m, 8)
    recipe = Recipe(
        title="Проверка",
        archived=False,
        version=1,
        content={
            "inputs": [{"material_id": m.id, "title": m.title, "quantity": "3"}],
            "outputs": [{"material_id": m.id, "title": m.title, "quantity": "1"}],
        },
    )
    db.add(recipe)
    db.commit()
    params = Manufacturing(warehouse=m.warehouse, multiplier=1, request_id=uuid4())
    plan = preview(db, recipe, params)
    assert (
        plan["inputs"][0]["stock"] == 10
        and plan["inputs"][0]["available_quantity"] == 2
        and plan["inputs"][0]["enough"] is False
    )
    with pytest.raises(HTTPException):
        manufacture(db, recipe.id, params, owner)
    db.rollback()
    db.refresh(m)
    assert m.quantity_in_stock == 10 and db.query(ManufactureRun).count() == 0
    with pytest.raises(ValueError, match="delta reconciliation"):
        import_export(db, {}, {}, {}, {"completed_at": "new"}, owner.id)


def test_negative_unreserved_stock_does_not_invent_a_reservation_deficit(
    db, api, make_user
):
    client, owner, m = setup(db, api, make_user, quantity=-2)
    result = client.get(f"/api/warehouse/materials/{m.id}").json()
    assert (
        result["reserved_quantity"] == 0
        and result["available_quantity"] == 0
        and result["reservation_shortage"] == 0
    )


def test_fractional_house_issue_normalizes_float_sum_to_stock_precision(
    db, api, make_user
):
    client, owner, m = setup(db, api, make_user, quantity=1, fractional=True)
    p, norm = house(db, m, need=0.1)
    block = ProductionBlock(production_id=p.id, name="Второй этап", sequence=2)
    db.add(block)
    db.flush()
    db.add(
        BlockMaterial(
            block_id=block.id,
            warehouse_material_id=m.id,
            inventory_number="",
            unit=m.unit,
            quantity_required=0.2,
            quantity_requested=0,
            quantity_provided=0,
        )
    )
    db.commit()
    reserve(client, m, "0.7")
    own, _ = reserve(client, m, "0.3", production_id=p.id, purpose="")
    response = client.post(
        f"/api/warehouse/techcard-issue/{p.id}", json={"received_by": "Цех"}
    )
    assert response.status_code == 200, response.text
    db.refresh(m)
    assert m.quantity_in_stock == 0.7
    assert (
        client.get(f"/api/warehouse/reservations/{own['id']}").json()["status"]
        == "issued"
    )


def reserve(client, m, q=6, **kw):
    body = {
        "request_id": str(uuid4()),
        "material_id": m.id,
        "quantity": str(q),
        "purpose": "Заказ цеха",
        **kw,
    }
    r = client.post("/api/warehouse/reservations", json=body)
    assert r.status_code == 201, r.text
    return r.json(), body


def change(row, q, **kw):
    return {
        "request_id": str(uuid4()),
        "version": row["version"],
        "quantity": str(q),
        "note": "Рабочее изменение",
        **kw,
    }


def house(db, m, need=6):
    cycle = Cycle(status=CycleStatus.PRODUCTION)
    db.add(cycle)
    db.flush()
    p = Production(cycle_id=cycle.id, house_index=1, name="Дом")
    db.add(p)
    db.flush()
    block = ProductionBlock(production_id=p.id, name="Каркас", sequence=1)
    db.add(block)
    db.flush()
    norm = BlockMaterial(
        block_id=block.id,
        warehouse_material_id=m.id,
        inventory_number="",
        unit=m.unit,
        quantity_required=need,
        quantity_requested=0,
        quantity_provided=0,
    )
    db.add(norm)
    db.commit()
    return p, norm


def test_partial_release_and_issue_are_audited_and_retries_do_not_change_stock_twice(
    db, api, make_user
):
    client, owner, m = setup(db, api, make_user)
    row, body = reserve(client, m)
    assert (
        client.post("/api/warehouse/reservations", json=body).json()["id"] == row["id"]
    )
    assert (
        client.post(
            "/api/warehouse/reservations", json={**body, "quantity": "5"}
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/api/warehouse/reservations",
            json={**body, "request_id": str(uuid4()), "quantity": "5"},
        ).status_code
        == 409
    )
    material = client.get(f"/api/warehouse/materials/{m.id}").json()
    assert (
        material["quantity_in_stock"],
        material["reserved_quantity"],
        material["available_quantity"],
    ) == (10, 6, 4)
    assert db.query(StockMovement).count() == 0
    release = change(row, 2)
    row = client.post(
        f"/api/warehouse/reservations/{row['id']}/release", json=release
    ).json()
    assert row["remaining_quantity"] == 4
    assert (
        client.post(
            f"/api/warehouse/reservations/{row['id']}/release", json=release
        ).json()["version"]
        == row["version"]
    )
    body = change(row, 3, received_by="Цех")
    row = client.post(
        f"/api/warehouse/reservations/{row['id']}/issue", json=body
    ).json()
    assert row["remaining_quantity"] == 1 and row["consumed_quantity"] == 3
    assert (
        client.post(
            f"/api/warehouse/reservations/{row['id']}/issue", json=body
        ).status_code
        == 200
    )
    db.refresh(m)
    assert m.quantity_in_stock == 7
    assert (
        db.query(StockMovement).count() == 1 and db.query(ReservationEvent).count() == 3
    )
    assert client.get(f"/api/warehouse/reservations/{row['id']}").json()["events"][-1][
        "movement_id"
    ]


def test_other_issues_transfers_and_returns_cannot_take_reserved_stock(
    db, api, make_user
):
    from app.warehouse.service import create_stock_operation
    from app.warehouse.models import WarehouseOperationKind, Supplier
    from app.purchasing.models import PurchaseReceipt

    client, owner, m = setup(db, api, make_user)
    reserve(client, m, 8)
    r = client.post(
        "/api/warehouse/operations/issue",
        json={
            "destination_kind": "object",
            "destination": "Объект",
            "received_by": "Цех",
            "lines": [{"warehouse_material_id": m.id, "quantity": 3}],
        },
    )
    assert r.status_code == 400
    doc = client.post(
        "/api/warehouse/documents",
        json={
            "request_id": str(uuid4()),
            "kind": "transfer",
            "warehouse": m.warehouse.value,
            "target_warehouse": Warehouse.ID_GROUP.value,
            "note": "Перемещение",
            "lines": [{"material_id": m.id, "quantity": 3}],
        },
    ).json()
    assert (
        client.post(
            f"/api/warehouse/documents/{doc['id']}/post",
            json={"version": 1, "request_id": str(uuid4())},
        ).status_code
        == 409
    )
    db.rollback()
    supplier = Supplier(name="Поставщик", categories=[], contacts=[])
    db.add(supplier)
    db.flush()
    receipt = PurchaseReceipt(
        supplier_id=supplier.id,
        actor_id=owner.id,
        number="ПР-1",
        status="posted",
        occurred_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        lines=[
            {
                "material_id": m.id,
                "quantity": 10,
                "unit_price": 10,
                "title": m.title,
                "unit": m.unit,
                "stock_tracked": True,
            }
        ],
    )
    db.add(receipt)
    db.commit()
    assert (
        client.post(
            "/api/purchasing/returns",
            json={
                "request_id": str(uuid4()),
                "receipt_id": receipt.id,
                "occurred_at": datetime.now(timezone.utc).isoformat(),
                "note": "Брак",
                "lines": [{"receipt_line": 0, "quantity": 3}],
            },
        ).status_code
        == 409
    )
    db.rollback()
    db.refresh(m)
    assert m.quantity_in_stock == 10 and db.query(StockMovement).count() == 0


def test_house_issue_uses_its_reserves_and_cannot_take_another_house_allocation(
    db, api, make_user
):
    from app.warehouse.service import issue_by_techcard

    client, owner, m = setup(db, api, make_user)
    p, norm = house(db, m)
    other, _ = reserve(client, m, 4)
    own, _ = reserve(client, m, 6, production_id=p.id, purpose="")
    preview = client.get(f"/api/warehouse/techcard-issue/{p.id}/preview").json()
    assert preview["lines"][0]["available_quantity"] == 6
    assert preview["lines"][0]["shortage"] is False
    result = client.post(
        f"/api/warehouse/techcard-issue/{p.id}", json={"received_by": "Каркасный цех"}
    )
    assert result.status_code == 200, result.text
    db.refresh(m)
    db.refresh(norm)
    assert m.quantity_in_stock == 4 and norm.quantity_provided == 6
    assert (
        client.get(f"/api/warehouse/reservations/{own['id']}").json()["status"]
        == "issued"
    )
    assert (
        client.get(f"/api/warehouse/reservations/{other['id']}").json()[
            "remaining_quantity"
        ]
        == 4
    )


def test_request_partial_issue_consumes_reserve_without_double_counting_planned_demand(
    db, api, make_user
):
    from app.production.service import request_material
    from app.warehouse.service import approve_request

    client, owner, m = setup(db, api, make_user)
    p, norm = house(db, m)
    request = request_material(db, norm, 6, owner)
    db.commit()
    own, _ = reserve(client, m, 6, production_id=p.id, purpose="")
    m.threshold = 1
    db.commit()
    assert (
        client.get(f"/api/warehouse/materials/{m.id}").json()["needs_supply"] is False
    )
    approve_request(db, request, owner, quantity=Decimal(3))
    db.commit()
    row = client.get(f"/api/warehouse/reservations/{own['id']}").json()
    assert row["remaining_quantity"] == 3 and row["consumed_quantity"] == 3
    db.refresh(m)
    assert m.quantity_in_stock == 7


def test_physical_count_can_reveal_a_shortage_but_never_silently_drops_holds(
    db, api, make_user
):
    client, owner, m = setup(db, api, make_user)
    row, _ = reserve(client, m, 8)
    doc = client.post(
        "/api/warehouse/documents",
        json={
            "request_id": str(uuid4()),
            "kind": "inventory",
            "warehouse": m.warehouse.value,
            "note": "Пересчёт",
            "lines": [{"material_id": m.id, "quantity": 5}],
        },
    ).json()
    assert (
        client.post(
            f"/api/warehouse/documents/{doc['id']}/post",
            json={"version": 1, "request_id": str(uuid4())},
        ).status_code
        == 200
    )
    result = client.get(f"/api/warehouse/materials/{m.id}").json()
    assert (
        result["reserved_quantity"] == 8
        and result["available_quantity"] == 0
        and result["reservation_shortage"] == 3
    )
    assert (
        client.get(f"/api/warehouse/reservations/{row['id']}").json()[
            "remaining_quantity"
        ]
        == 8
    )


def test_source_order_capacity_includes_locally_issued_quantities(db, api, make_user):
    from app.moysklad.models import SourceRecord, SourceLink

    client, owner, m = setup(db, api, make_user)
    source = SourceRecord(
        entity_type="customerorder",
        source_id="order-1",
        title="1",
        archived=False,
        search_text="",
        payload={
            "applicable": True,
            "store": {"id": "store-1"},
            "positions": {
                "rows": [
                    {"assortment": {"id": "product-1"}, "quantity": 5, "shipped": 1}
                ]
            },
        },
    )
    db.add_all(
        [
            source,
            SourceLink(
                source_key="stock/product-1/store-1",
                target_type="warehouse_materials",
                target_id=m.id,
            ),
        ]
    )
    db.commit()
    row, _ = reserve(client, m, 4, source_order_id=source.id, source_line=0, purpose="")
    row = client.post(
        f"/api/warehouse/reservations/{row['id']}/issue",
        json=change(row, 2, received_by="Покупатель"),
    ).json()
    row = client.post(
        f"/api/warehouse/reservations/{row['id']}/release", json=change(row, 2)
    ).json()
    body = {
        "request_id": str(uuid4()),
        "material_id": m.id,
        "quantity": "3",
        "source_order_id": source.id,
        "source_line": 0,
    }
    assert client.post("/api/warehouse/reservations", json=body).status_code == 409
    body["quantity"] = "2"
    body["request_id"] = str(uuid4())
    assert client.post("/api/warehouse/reservations", json=body).status_code == 201


def test_permissions_precision_versions_and_archive_keep_history(db, api, make_user):
    client, owner, m = setup(db, api, make_user)
    row, body = reserve(client, m, 4)
    viewer = make_user(Module.WAREHOUSE, level=AccessLevel.VIEW)
    assert api(viewer).get("/api/warehouse/reservations").status_code == 200
    assert api(viewer).post("/api/warehouse/reservations", json=body).status_code == 403
    client = api(owner)
    assert (
        client.patch(f"/api/warehouse/materials/{m.id}", json={"unit": "м"}).status_code
        == 409
    )
    assert (
        client.post(
            f"/api/warehouse/reservations/{row['id']}/archive",
            json={"version": 1, "archived": True},
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"/api/warehouse/reservations/{row['id']}/release", json=change(row, "0.5")
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"/api/warehouse/reservations/{row['id']}/release",
            json=change(row, 1, version=99),
        ).status_code
        == 409
    )
    row = client.post(
        f"/api/warehouse/reservations/{row['id']}/release", json=change(row, 4)
    ).json()
    assert (
        client.post(
            f"/api/warehouse/reservations/{row['id']}/archive",
            json={"version": row["version"], "archived": True},
        ).status_code
        == 200
    )
    assert (
        client.get("/api/warehouse/reservations?archive=archived&status=closed").json()[
            "total"
        ]
        == 1
    )
    assert db.query(ReservationEvent).count() == 2
