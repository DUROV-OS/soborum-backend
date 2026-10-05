"""Документы операций склада и журнал (0088-a):

- оприходование и списание проводятся документом из нескольких строк,
  у каждой строки фиксируется остаток после;
- списание не уводит остаток в минус и не проводится частично;
- причина обязательна, дата операции не в будущем, недробный материал — целым;
- списание из карточки материала (0030-e) тоже оформляется документом;
- журнал показывает только движения остатка, фильтруется по направлению и дому,
  заявка производства попадает в журнал с домом в «куда».
"""

from datetime import datetime, timedelta, timezone

from app.common.module_access import AccessLevel, Module
from app.cycle.models import Cycle, CycleStatus
from app.production import service as production_service
from app.production.models import BlockMaterial, Production, ProductionBlock
from app.warehouse.models import MaterialCategory, StockMovement, Warehouse, WarehouseMaterial


def _material(db, title="Брус", stock=10, is_fractional=False):
    material = WarehouseMaterial(
        warehouse=Warehouse.TECHNOLOGY,
        category=MaterialCategory.NONE,
        title=title,
        code=f"M-{title}",
        unit="шт",
        is_fractional=is_fractional,
        quantity_in_stock=stock,
    )
    db.add(material)
    db.flush()
    return material


def _stock(db, material):
    db.expire_all()
    return float(db.get(WarehouseMaterial, material.id).quantity_in_stock)


def test_receipt_document_with_two_lines_and_balance_after(api, make_user, db):
    beam = _material(db, "Брус", stock=10)
    board = _material(db, "Доска", stock=4)
    db.commit()
    admin = api(make_user(admin=True))

    resp = admin.post(
        "/api/warehouse/operations/receipt",
        json={
            "note": "излишек при пересчёте",
            "lines": [
                {"warehouse_material_id": beam.id, "quantity": 2},
                {"warehouse_material_id": board.id, "quantity": 3},
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "receipt"
    assert body["note"] == "излишек при пересчёте"
    assert [(line["delta"], line["balance_after"]) for line in body["lines"]] == [(2, 12), (3, 7)]
    assert _stock(db, beam) == 12
    assert _stock(db, board) == 7

    journal = admin.get("/api/warehouse/journal").json()
    assert {row["operation_id"] for row in journal} == {body["id"]}
    assert {row["reason"] for row in journal} == {"receipt"}


def test_write_off_shortage_rejects_whole_document(api, make_user, db):
    beam = _material(db, "Брус", stock=10)
    board = _material(db, "Доска", stock=1)
    db.commit()
    admin = api(make_user(admin=True))

    resp = admin.post(
        "/api/warehouse/operations/write-off",
        json={
            "note": "брак",
            "lines": [
                {"warehouse_material_id": beam.id, "quantity": 2},
                {"warehouse_material_id": board.id, "quantity": 5},
            ],
        },
    )
    assert resp.status_code == 400
    assert "Доска" in resp.json()["detail"]
    assert _stock(db, beam) == 10
    assert _stock(db, board) == 1
    assert db.query(StockMovement).count() == 0


def test_write_off_requires_reason_and_full_level(api, make_user, db):
    beam = _material(db)
    db.commit()
    line = [{"warehouse_material_id": beam.id, "quantity": 1}]

    editor = api(make_user(Module.WAREHOUSE, level=AccessLevel.EDIT))
    assert editor.post("/api/warehouse/operations/write-off", json={"note": "брак", "lines": line}).status_code == 403

    admin = api(make_user(admin=True))
    resp = admin.post("/api/warehouse/operations/write-off", json={"note": "  ", "lines": line})
    assert resp.status_code == 400
    assert "причину" in resp.json()["detail"]
    assert _stock(db, beam) == 10


def test_operation_validations(api, make_user, db):
    beam = _material(db, "Брус", stock=10)
    sand = _material(db, "Песок", stock=10, is_fractional=True)
    db.commit()
    admin = api(make_user(admin=True))

    def post(lines, **extra):
        return admin.post("/api/warehouse/operations/receipt", json={"note": "пересчёт", "lines": lines, **extra})

    assert post([]).status_code == 400
    assert post([{"warehouse_material_id": beam.id, "quantity": 0}]).status_code == 400
    assert post([{"warehouse_material_id": beam.id, "quantity": 1.5}]).status_code == 400
    assert (
        post([{"warehouse_material_id": beam.id, "quantity": 1}, {"warehouse_material_id": beam.id, "quantity": 1}])
        .status_code
        == 400
    )
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    assert post([{"warehouse_material_id": beam.id, "quantity": 1}], occurred_at=future).status_code == 400
    assert _stock(db, beam) == 10

    assert post([{"warehouse_material_id": sand.id, "quantity": 1.5}]).status_code == 200
    assert _stock(db, sand) == 11.5


def test_occurred_at_in_past_is_kept_in_journal(api, make_user, db):
    beam = _material(db)
    db.commit()
    admin = api(make_user(admin=True))
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)

    resp = admin.post(
        "/api/warehouse/operations/write-off",
        json={
            "note": "недостача",
            "occurred_at": yesterday.isoformat(),
            "lines": [{"warehouse_material_id": beam.id, "quantity": 1}],
        },
    )
    assert resp.status_code == 200
    row = admin.get("/api/warehouse/journal").json()[0]
    occurred = datetime.fromisoformat(row["occurred_at"].replace("Z", "+00:00"))
    if occurred.tzinfo is None:
        occurred = occurred.replace(tzinfo=timezone.utc)
    assert abs((occurred - yesterday).total_seconds()) < 1
    assert row["balance_after"] == 9
    assert row["note"] == "недостача"
    assert row["created_by_name"] == "Тестовый сотрудник"


def test_card_write_off_creates_document(api, make_user, db):
    beam = _material(db)
    db.commit()
    admin = api(make_user(admin=True))

    resp = admin.post(f"/api/warehouse/materials/{beam.id}/write-off", json={"quantity": 3, "reason": "брак"})
    assert resp.status_code == 200
    row = admin.get("/api/warehouse/journal").json()[0]
    assert row["reason"] == "write_off"
    assert row["operation_id"] is not None
    assert row["balance_after"] == 7
    operation = admin.get(f"/api/warehouse/operations/{row['operation_id']}").json()
    assert operation["kind"] == "write_off"
    assert operation["note"] == "брак"


def _production_with_request(db, user, material, quantity):
    cycle = Cycle(status=CycleStatus.PRODUCTION)
    db.add(cycle)
    db.flush()
    production = Production(cycle_id=cycle.id, house_index=1, name="Дом")
    db.add(production)
    db.flush()
    block = ProductionBlock(production_id=production.id, name="Каркас", sequence=1)
    db.add(block)
    db.flush()
    block_material = BlockMaterial(
        block_id=block.id, warehouse_material_id=material.id, inventory_number="", unit="шт",
        quantity_required=quantity, quantity_requested=0, quantity_provided=0,
    )
    db.add(block_material)
    db.flush()
    request = production_service.request_material(db, block_material, quantity, user)
    db.commit()
    return production, request


def test_journal_direction_house_filter_and_excludes_non_stock_rows(api, make_user, db):
    beam = _material(db, "Брус", stock=10)
    db.commit()
    admin_user = make_user(admin=True)
    admin = api(admin_user)

    production, request = _production_with_request(db, admin_user, beam, 4)
    assert admin.post(f"/api/warehouse/requests/{request.id}/approve").status_code == 200
    admin.post(
        "/api/warehouse/operations/receipt",
        json={"note": "излишек", "lines": [{"warehouse_material_id": beam.id, "quantity": 1}]},
    )

    everything = admin.get("/api/warehouse/journal").json()
    assert {row["reason"] for row in everything} == {"issued", "receipt"}

    outgoing = admin.get("/api/warehouse/journal", params={"direction": "out"}).json()
    assert len(outgoing) == 1
    issued = outgoing[0]
    assert issued["reason"] == "issued"
    assert issued["delta"] == -4
    assert issued["balance_after"] == 6
    assert issued["production_id"] == production.id
    assert issued["destination_kind"] == "house"
    assert issued["destination"] == "Дом"

    by_house = admin.get("/api/warehouse/journal", params={"production_id": production.id}).json()
    assert [row["movement_id"] for row in by_house] == [issued["movement_id"]]

    incoming = admin.get("/api/warehouse/journal", params={"direction": "in"}).json()
    assert [row["reason"] for row in incoming] == ["receipt"]
