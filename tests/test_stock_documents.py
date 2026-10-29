from decimal import Decimal
from uuid import uuid4
from app.warehouse.models import (
    Warehouse,
    WarehouseMaterial,
    MaterialCategory,
    StockMovement,
    StockDocument,
)
from app.common.module_access import Module, AccessLevel
from app.moysklad.models import SourceRecord, SourceLink


def setup(db, api, make_user, target=True):
    owner = make_user(admin=True)
    a = WarehouseMaterial(
        warehouse=Warehouse.TECHNOLOGY,
        title="Брус",
        code="STOCK-DOC-TEST",
        unit="м³",
        category=MaterialCategory.NONE,
        is_fractional=True,
        quantity_in_stock=10,
        purchase_price=20,
    )
    b = WarehouseMaterial(
        warehouse=Warehouse.ID_GROUP,
        title="Брус",
        code="STOCK-DOC-TEST",
        unit="м³",
        category=MaterialCategory.NONE,
        is_fractional=True,
        quantity_in_stock=2,
        purchase_price=10,
    )
    db.add(a)
    if target:
        db.add(b)
    db.commit()
    return api(owner), owner, a, b


def create(client, a, kind="transfer", quantity="3", **kw):
    body = dict(
        request_id=str(uuid4()),
        kind=kind,
        warehouse=a.warehouse.value,
        note="Рабочий пересчёт",
        lines=[dict(material_id=a.id, quantity=quantity)],
        **kw,
    )
    if kind == "transfer":
        body["target_warehouse"] = Warehouse.ID_GROUP.value
    response = client.post("/api/warehouse/documents", json=body)
    assert response.status_code == 201, response.text
    return response.json(), body


def post(client, doc, **kw):
    return client.post(
        f"/api/warehouse/documents/{doc['id']}/post",
        json={"version": doc["version"], "request_id": str(uuid4()), **kw},
    )


def test_transfer_is_atomic_retains_value_and_retry_does_not_duplicate(
    db, api, make_user
):
    client, owner, a, b = setup(db, api, make_user)
    doc, body = create(client, a)
    assert client.post("/api/warehouse/documents", json=body).json()["id"] == doc["id"]
    body["note"] = "Другие данные"
    assert client.post("/api/warehouse/documents", json=body).status_code == 409
    key = str(uuid4())
    response = post(client, doc, request_id=key)
    assert response.status_code == 200, response.text
    assert post(client, doc, request_id=key).json() == response.json()
    assert post(client, doc).status_code == 409
    db.refresh(a)
    db.refresh(b)
    assert (
        a.quantity_in_stock == 7 and b.quantity_in_stock == 5 and b.purchase_price == 16
    )
    assert (
        Decimal(str(a.quantity_in_stock)) * Decimal(str(a.purchase_price))
        + Decimal(str(b.quantity_in_stock)) * Decimal(str(b.purchase_price))
        == 220
    )
    assert db.query(StockMovement).count() == 2
    assert {x["reason"] for x in client.get("/api/warehouse/journal").json()} == {
        "transfer_in",
        "transfer_out",
    }
    assert len(response.json()["operation_ids"]) == 2
    assert (
        client.patch(
            f"/api/warehouse/documents/{doc['id']}",
            json={
                "version": 2,
                "note": "Изменить",
                "lines": [{"material_id": a.id, "quantity": "2"}],
            },
        ).status_code
        == 409
    )


def test_missing_target_created_only_on_post_and_insufficient_stock_is_not_replayed(
    db, api, make_user
):
    client, owner, a, _ = setup(db, api, make_user, target=False)
    doc, _ = create(client, a, quantity="11")
    assert db.query(WarehouseMaterial).count() == 1
    assert post(client, doc).status_code == 409
    db.rollback()
    assert (
        db.query(WarehouseMaterial).count() == 1
        and db.query(StockMovement).count() == 0
    )
    changed = client.patch(
        f"/api/warehouse/documents/{doc['id']}",
        json={
            "version": 1,
            "note": "Пополнить другой склад",
            "lines": [{"material_id": a.id, "quantity": "2.5"}],
        },
    ).json()
    assert post(client, changed).status_code == 200
    target = db.query(WarehouseMaterial).filter_by(warehouse=Warehouse.ID_GROUP).one()
    assert target.quantity_in_stock == 2.5 and target.purchase_price == 20


def test_count_detects_intervening_work_recount_clears_actuals_and_requires_full(
    db, api, make_user
):
    client, owner, a, b = setup(db, api, make_user)
    doc, _ = create(client, a, kind="inventory", quantity="8")
    a.quantity_in_stock = 9
    db.commit()
    assert post(client, doc).status_code == 409
    db.rollback()
    db.refresh(a)
    assert a.quantity_in_stock == 9
    reset = client.post(
        f"/api/warehouse/documents/{doc['id']}/recount", json={"version": 1}
    ).json()
    assert (
        reset["lines"][0]["quantity"] is None
        and Decimal(reset["lines"][0]["expected_quantity"]) == 9
    )
    assert post(client, reset).status_code == 422
    changed = client.patch(
        f"/api/warehouse/documents/{doc['id']}",
        json={
            "version": reset["version"],
            "note": "Фактический пересчёт",
            "lines": [{"material_id": a.id, "quantity": "0"}],
        },
    ).json()
    editor = make_user(Module.WAREHOUSE, level=AccessLevel.EDIT)
    assert post(api(editor), changed).status_code == 403
    client = api(owner)
    assert post(client, changed).status_code == 200
    db.refresh(a)
    assert a.quantity_in_stock == 0
    assert db.query(StockMovement).one().delta == -9


def test_permissions_fraction_precision_versions_and_archive_do_not_change_stock(
    db, api, make_user
):
    client, owner, a, b = setup(db, api, make_user)
    doc, body = create(client, a)
    viewer = make_user(Module.WAREHOUSE, level=AccessLevel.VIEW)
    assert api(viewer).get("/api/warehouse/documents").status_code == 200
    assert (
        api(viewer)
        .post("/api/warehouse/documents", json={**body, "request_id": str(uuid4())})
        .status_code
        == 403
    )
    client = api(owner)
    assert (
        client.patch(
            f"/api/warehouse/documents/{doc['id']}",
            json={"version": 99, "note": "Причина", "lines": body["lines"]},
        ).status_code
        == 409
    )
    archived = client.post(
        f"/api/warehouse/documents/{doc['id']}/archive",
        json={"version": 1, "archived": True},
    ).json()
    assert post(client, archived).status_code == 409
    assert client.get("/api/warehouse/documents?archive=archived").json()["total"] == 1
    for qty in ("NaN", "Infinity", "-1", "0.0000001"):
        assert (
            client.post(
                "/api/warehouse/documents",
                json={
                    **body,
                    "request_id": str(uuid4()),
                    "lines": [{"material_id": a.id, "quantity": qty}],
                },
            ).status_code
            == 422
        )
    a.is_fractional = False
    db.commit()
    assert (
        client.post(
            "/api/warehouse/documents",
            json={
                **body,
                "request_id": str(uuid4()),
                "lines": [{"material_id": a.id, "quantity": "1.2"}],
            },
        ).status_code
        == 422
    )
    assert db.query(StockMovement).count() == 0


def test_import_history_keeps_archive_and_never_replays_stock(db, api, make_user):
    from app.warehouse.document_import import import_history

    client, owner, a, b = setup(db, api, make_user)
    db.add_all(
        [
            SourceRecord(
                entity_type="store",
                source_id="s1",
                title=a.warehouse.value,
                archived=False,
                search_text="",
                payload={},
            ),
            SourceRecord(
                entity_type="store",
                source_id="s2",
                title=b.warehouse.value,
                archived=False,
                search_text="",
                payload={},
            ),
            SourceRecord(
                entity_type="move",
                source_id="move1",
                title="М-001",
                archived=True,
                search_text="",
                payload={
                    "sourceStore": {"id": "s1"},
                    "targetStore": {"id": "s2"},
                    "applicable": True,
                    "moment": "2026-10-01 10:00:00",
                    "positions": {
                        "rows": [
                            {
                                "assortment": {"id": "prod1"},
                                "quantity": 3,
                                "price": 2000,
                            }
                        ]
                    },
                },
            ),
            SourceLink(
                source_key="stock/prod1/s1",
                target_type="warehouse_materials",
                target_id=a.id,
            ),
            SourceLink(
                source_key="stock/prod1/s2",
                target_type="warehouse_materials",
                target_id=b.id,
            ),
        ]
    )
    db.commit()
    assert import_history(db, owner.id) == {"transfer": 1, "inventory": 0}
    db.commit()
    assert import_history(db, owner.id) == {"transfer": 0, "inventory": 0}
    history = db.query(StockDocument).one()
    assert (
        history.archived
        and history.status == "historical"
        and history.lines[0]["target_material_id"] == b.id
    )
    assert db.query(StockMovement).count() == 0
    assert a.quantity_in_stock == 10 and b.quantity_in_stock == 2
    assert post(client, {"id": history.id, "version": 1}).status_code == 409


def test_count_rejects_a_movement_even_when_the_balance_returns_to_its_start(
    db, api, make_user
):
    from app.warehouse.service import create_stock_operation
    from app.warehouse.models import WarehouseOperationKind

    client, owner, a, b = setup(db, api, make_user)
    doc, _ = create(client, a, kind="inventory", quantity="10")
    create_stock_operation(
        db,
        WarehouseOperationKind.WRITE_OFF,
        [(a.id, 1)],
        owner,
        note="Выдача во время пересчёта",
    )
    create_stock_operation(
        db,
        WarehouseOperationKind.RECEIPT,
        [(a.id, 1)],
        owner,
        note="Возврат во время пересчёта",
    )
    db.commit()
    assert post(client, doc).status_code == 409
    assert db.query(StockDocument).one().status == "draft"


def test_source_identity_handles_different_local_codes_and_a_count_posts_both_directions(
    db, api, make_user
):
    client, owner, a, b = setup(db, api, make_user)
    b.code = "ДРУГОЙ-ЛОКАЛЬНЫЙ-КОД"
    db.add_all(
        [
            SourceLink(
                source_key="stock/same-product/a",
                target_type="warehouse_materials",
                target_id=a.id,
            ),
            SourceLink(
                source_key="stock/same-product/b",
                target_type="warehouse_materials",
                target_id=b.id,
            ),
        ]
    )
    c = WarehouseMaterial(
        warehouse=Warehouse.TECHNOLOGY,
        title="Штуки",
        code="COUNT-WHOLE",
        unit="шт",
        is_fractional=False,
        category=MaterialCategory.NONE,
        quantity_in_stock=3,
        purchase_price=5,
    )
    db.add(c)
    db.commit()
    doc, _ = create(client, a)
    assert post(client, doc).status_code == 200
    db.refresh(b)
    assert b.quantity_in_stock == 5
    doc, _ = create(client, a, kind="inventory", quantity="9")
    changed = client.patch(
        f"/api/warehouse/documents/{doc['id']}",
        json={
            "version": 1,
            "note": "Пересчёт двух позиций",
            "lines": [
                {"material_id": a.id, "quantity": "9"},
                {"material_id": c.id, "quantity": "0"},
            ],
        },
    ).json()
    result = post(client, changed)
    assert result.status_code == 200, result.text
    assert len(result.json()["operation_ids"]) == 2
    db.refresh(a)
    db.refresh(c)
    assert a.quantity_in_stock == 9 and c.quantity_in_stock == 0
