"""Отпуск со склада (0088-b):

- ручной отпуск требует «куда» и «кто получил», для дома подставляет его
  название, нормативы производства не меняет;
- отпуск по техкарте: предпросмотр с остатком после и признаком нехватки,
  нехватка блокирует проведение целиком, проведение переносит норматив из
  «нужно» в «выдано» по блокам в порядке этапов, заявки на одобрении не трогает;
- доступ — уровень edit на склад.
"""

from app.common.module_access import AccessLevel, Module
from app.cycle.models import Cycle, CycleStatus
from app.production.models import BlockMaterial, Production, ProductionBlock
from app.tasks.models import Task, TaskLinkType, TaskStatus
from app.warehouse.models import MaterialCategory, StockMovement, Warehouse, WarehouseMaterial


def _material(db, title, stock):
    material = WarehouseMaterial(
        warehouse=Warehouse.TECHNOLOGY, category=MaterialCategory.NONE, title=title,
        code=f"M-{title}", unit="шт", quantity_in_stock=stock,
    )
    db.add(material)
    db.flush()
    return material


def _house(db, name="Дом"):
    cycle = Cycle(status=CycleStatus.PRODUCTION)
    db.add(cycle)
    db.flush()
    production = Production(cycle_id=cycle.id, house_index=1, name=name)
    db.add(production)
    db.flush()
    return production


def _block(db, production, name, sequence):
    block = ProductionBlock(production_id=production.id, name=name, sequence=sequence)
    db.add(block)
    db.flush()
    return block


def _norm(db, block, material, required, requested=0, provided=0):
    bm = BlockMaterial(
        block_id=block.id, warehouse_material_id=material.id, inventory_number="", unit="шт",
        quantity_required=required, quantity_requested=requested, quantity_provided=provided,
    )
    db.add(bm)
    db.flush()
    return bm


def _reload(db, obj):
    db.expire_all()
    return db.get(type(obj), obj.id)


# --- ручной отпуск ---


def test_manual_issue_to_workshop(api, make_user, db):
    beam = _material(db, "Брус", 10)
    db.commit()
    keeper = api(make_user(Module.WAREHOUSE, level=AccessLevel.EDIT))

    resp = keeper.post(
        "/api/warehouse/operations/issue",
        json={
            "destination_kind": "workshop",
            "destination": "Цех №2",
            "received_by": "Петров",
            "lines": [{"warehouse_material_id": beam.id, "quantity": 3}],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "issue_manual"
    assert body["destination"] == "Цех №2"
    assert body["received_by"] == "Петров"
    assert body["lines"][0]["balance_after"] == 7

    row = keeper.get("/api/warehouse/journal", params={"direction": "out"}).json()[0]
    assert (row["reason"], row["destination_kind"], row["destination"], row["received_by"]) == (
        "issued_manual", "workshop", "Цех №2", "Петров",
    )

    hints = keeper.get("/api/warehouse/operations/destinations", params={"kind": "workshop"}).json()
    assert hints == {"destinations": ["Цех №2"], "received_by": ["Петров"]}


def test_manual_issue_required_fields_and_access(api, make_user, db):
    beam = _material(db, "Брус", 10)
    db.commit()
    line = [{"warehouse_material_id": beam.id, "quantity": 1}]

    viewer = api(make_user(Module.WAREHOUSE, level=AccessLevel.VIEW))
    assert viewer.post(
        "/api/warehouse/operations/issue",
        json={"destination_kind": "object", "destination": "Объект", "received_by": "Петров", "lines": line},
    ).status_code == 403

    admin = api(make_user(admin=True))
    no_recipient = admin.post(
        "/api/warehouse/operations/issue",
        json={"destination_kind": "object", "destination": "Объект", "received_by": " ", "lines": line},
    )
    assert no_recipient.status_code == 400
    assert "кто получил" in no_recipient.json()["detail"]
    no_destination = admin.post(
        "/api/warehouse/operations/issue",
        json={"destination_kind": "rework", "received_by": "Петров", "lines": line},
    )
    assert no_destination.status_code == 400
    assert admin.post(
        "/api/warehouse/operations/issue",
        json={"destination_kind": "object", "destination": "Объект", "received_by": "Петров",
              "lines": [{"warehouse_material_id": beam.id, "quantity": 11}]},
    ).status_code == 400
    assert float(_reload(db, beam).quantity_in_stock) == 10


def test_manual_issue_to_house_keeps_norms(api, make_user, db):
    beam = _material(db, "Брус", 10)
    house = _house(db, "Дом 1")
    bm = _norm(db, _block(db, house, "Каркас", 1), beam, required=5)
    db.commit()
    admin = api(make_user(admin=True))

    resp = admin.post(
        "/api/warehouse/operations/issue",
        json={"destination_kind": "house", "production_id": house.id, "received_by": "Петров",
              "lines": [{"warehouse_material_id": beam.id, "quantity": 2}]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["destination"] == "Дом 1"
    assert resp.json()["production_id"] == house.id
    bm = _reload(db, bm)
    assert (float(bm.quantity_required), float(bm.quantity_provided)) == (5, 0)


# --- отпуск по техкарте ---


def _techcard_house(db):
    beam = _material(db, "Брус", 10)
    screws = _material(db, "Саморез", 3)
    house = _house(db, "Дом 1")
    frame = _block(db, house, "Каркас", 1)
    roof = _block(db, house, "Кровля", 2)
    beam_frame = _norm(db, frame, beam, required=4, requested=1, provided=2)
    beam_roof = _norm(db, roof, beam, required=2)
    screws_roof = _norm(db, roof, screws, required=5)
    db.commit()
    return house, beam, screws, beam_frame, beam_roof, screws_roof


def test_techcard_houses_and_preview(api, make_user, db):
    house, beam, screws, *_ = _techcard_house(db)
    empty_house = _house(db, "Дом без норм")
    _norm(db, _block(db, empty_house, "Каркас", 1), beam, required=0, provided=3)
    db.add(Task(title="Сопоставить", link_type=TaskLinkType.BLOCK_MATERIAL_MATCH,
                block_id=house.blocks[0].id, status=TaskStatus.READY))
    db.commit()
    admin = api(make_user(admin=True))

    houses = admin.get("/api/warehouse/techcard-issue/houses").json()
    assert [(h["production_id"], h["positions_to_issue"]) for h in houses] == [(house.id, 2)]
    every_house = admin.get("/api/warehouse/techcard-issue/houses", params={"all": True}).json()
    assert {(h["production_id"], h["positions_to_issue"]) for h in every_house} == {(house.id, 2), (empty_house.id, 0)}

    preview = admin.get(f"/api/warehouse/techcard-issue/{house.id}/preview").json()
    assert preview["house_label"] == "Дом 1"
    assert preview["unmatched_materials_count"] == 1
    lines = {line["material_title"]: line for line in preview["lines"]}
    assert lines["Брус"]["norm_total"] == 9
    assert (lines["Брус"]["provided"], lines["Брус"]["requested"], lines["Брус"]["to_issue"]) == (2, 1, 6)
    assert (lines["Брус"]["balance_after"], lines["Брус"]["shortage"]) == (4, False)
    assert (lines["Саморез"]["balance_after"], lines["Саморез"]["shortage"]) == (-2, True)


def test_techcard_issue_shortage_blocks_everything(api, make_user, db):
    house, beam, screws, *_ = _techcard_house(db)
    admin = api(make_user(admin=True))

    resp = admin.post(f"/api/warehouse/techcard-issue/{house.id}", json={"received_by": "Петров"})
    assert resp.status_code == 400
    assert "Саморез" in resp.json()["detail"]
    assert float(_reload(db, beam).quantity_in_stock) == 10
    assert db.query(StockMovement).count() == 0


def test_techcard_issue_with_reduced_lines_moves_norms(api, make_user, db):
    house, beam, screws, beam_frame, beam_roof, screws_roof = _techcard_house(db)
    admin = api(make_user(admin=True))

    too_much = admin.post(
        f"/api/warehouse/techcard-issue/{house.id}",
        json={"received_by": "Петров", "lines": [{"warehouse_material_id": beam.id, "quantity": 7}]},
    )
    assert too_much.status_code == 400

    resp = admin.post(
        f"/api/warehouse/techcard-issue/{house.id}",
        json={
            "received_by": "Петров",
            "lines": [
                {"warehouse_material_id": beam.id, "quantity": 5},
                {"warehouse_material_id": screws.id, "quantity": 3},
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["kind"], body["destination_kind"], body["production_id"]) == ("issue_techcard", "house", house.id)
    assert {line["material_title"]: line["balance_after"] for line in body["lines"]} == {"Брус": 5, "Саморез": 0}

    # 5 бруса: сначала весь «Каркас» (4), затем 1 из «Кровли»; заявка (1) не тронута.
    beam_frame, beam_roof, screws_roof = (_reload(db, x) for x in (beam_frame, beam_roof, screws_roof))
    assert (float(beam_frame.quantity_required), float(beam_frame.quantity_requested),
            float(beam_frame.quantity_provided)) == (0, 1, 6)
    assert (float(beam_roof.quantity_required), float(beam_roof.quantity_provided)) == (1, 1)
    assert (float(screws_roof.quantity_required), float(screws_roof.quantity_provided)) == (2, 3)

    journal = admin.get("/api/warehouse/journal", params={"production_id": house.id}).json()
    assert {row["reason"] for row in journal} == {"issued_techcard"}
    assert {row["received_by"] for row in journal} == {"Петров"}


def test_techcard_issue_requires_recipient(api, make_user, db):
    house, beam, *_ = _techcard_house(db)
    admin = api(make_user(admin=True))
    resp = admin.post(
        f"/api/warehouse/techcard-issue/{house.id}",
        json={"received_by": "", "lines": [{"warehouse_material_id": beam.id, "quantity": 1}]},
    )
    assert resp.status_code == 400
    assert float(_reload(db, beam).quantity_in_stock) == 10
