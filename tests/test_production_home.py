"""«Главная» вкладка одного производства (0065-a): «Требует внимания» и
«Актуальное» пересчитаны по одному циклу/дому, а не по всей компании;
документы клиента (`house_model`/`ar_file`/`kr_file`/`house_project_file`)
отдаются без цены/контактов/адреса — право `production` не должно их
раскрывать."""

from datetime import datetime, timezone

from app.clients.models import Client
from app.common.files import FileAsset, FilePurpose
from app.common.module_access import Module
from app.cycle.models import Cycle, CycleStatus
from app.production.models import BlockMaterial, MaterialRequest, MaterialRequestStatus, Production, ProductionBlock
from app.tasks.models import Task, TaskStatus
from app.warehouse.models import MaterialCategory, Warehouse, WarehouseMaterial


def _make_production_with_client(db, **client_fields):
    cycle = Cycle(status=CycleStatus.PRODUCTION)
    db.add(cycle)
    db.flush()
    client = Client(
        cycle_id=cycle.id,
        full_name="Иван Клиентов",
        phone="+79160000000",
        email="ivan@example.com",
        final_price=5_000_000,
        installation_address="Московская обл., д. Тестово",
        **client_fields,
    )
    db.add(client)
    production = Production(cycle_id=cycle.id, house_index=1, name="Дом")
    db.add(production)
    db.flush()
    return production


def _make_block(db, production):
    block = ProductionBlock(production_id=production.id, name="Блок")
    db.add(block)
    db.flush()
    return block


def _make_warehouse_material(db):
    material = WarehouseMaterial(
        warehouse=Warehouse.TECHNOLOGY,
        category=MaterialCategory.NONE,
        title="Брус",
        code=f"M-{db.query(WarehouseMaterial).count()}",
        unit="шт",
        quantity_in_stock=100,
    )
    db.add(material)
    db.flush()
    return material


def test_home_has_no_signals_for_clean_production(api, make_user, db):
    production = _make_production_with_client(db)
    db.commit()
    worker = api(make_user(Module.PRODUCTION))
    resp = worker.get(f"/api/production/{production.id}/home")
    assert resp.status_code == 200
    body = resp.json()
    assert body["actions"] == []


def test_home_reports_pending_material_request_of_this_production_only(api, make_user, db):
    production = _make_production_with_client(db)
    other_production = _make_production_with_client(db)
    block = _make_block(db, production)
    other_block = _make_block(db, other_production)
    warehouse_material = _make_warehouse_material(db)
    requester = make_user(Module.PRODUCTION)

    material = BlockMaterial(
        block_id=block.id, warehouse_material_id=warehouse_material.id,
        inventory_number="INV-1", unit="шт", quantity_required=5,
    )
    other_material = BlockMaterial(
        block_id=other_block.id, warehouse_material_id=warehouse_material.id,
        inventory_number="INV-2", unit="шт", quantity_required=0,
    )
    db.add_all([material, other_material])
    db.flush()
    db.add(MaterialRequest(
        block_material_id=material.id, warehouse_material_id=warehouse_material.id,
        quantity=3, status=MaterialRequestStatus.PENDING, requested_by_id=requester.id,
    ))
    db.commit()

    worker = api(make_user(Module.PRODUCTION))
    resp = worker.get(f"/api/production/{production.id}/home")
    assert resp.status_code == 200
    action_ids = {a["id"] for a in resp.json()["actions"]}
    assert "production:pending_material_requests" in action_ids

    other_resp = worker.get(f"/api/production/{other_production.id}/home")
    assert other_resp.json()["actions"] == []


def test_home_reports_overdue_task_of_this_production(api, make_user, db):
    production = _make_production_with_client(db)
    block = _make_block(db, production)
    assignee = make_user(Module.PRODUCTION)
    overdue_task = Task(
        title="Просроченная задача", block_id=block.id, status=TaskStatus.READY,
        deadline=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    overdue_task.assignees = [assignee]
    db.add(overdue_task)
    db.commit()

    worker = api(make_user(Module.PRODUCTION))
    resp = worker.get(f"/api/production/{production.id}/home")
    action_ids = {a["id"] for a in resp.json()["actions"]}
    assert "production:overdue_tasks" in action_ids


def test_home_documents_expose_only_house_and_files_not_price_or_contacts(api, make_user, db):
    admin = make_user(admin=True)
    ar_asset = FileAsset(
        filename="ar.pdf", content_type="application/pdf", path_on_disk="/tmp/ar.pdf",
        purpose=FilePurpose.ARCHITECTURAL_DECISIONS, uploaded_by_id=admin.id,
    )
    kr_asset = FileAsset(
        filename="kr.pdf", content_type="application/pdf", path_on_disk="/tmp/kr.pdf",
        purpose=FilePurpose.CONSTRUCTIVE_DECISIONS, uploaded_by_id=admin.id,
    )
    db.add_all([ar_asset, kr_asset])
    db.flush()
    production = _make_production_with_client(db, ar_file_id=ar_asset.id, kr_file_id=kr_asset.id)
    db.commit()

    worker = api(make_user(Module.PRODUCTION))
    resp = worker.get(f"/api/production/{production.id}/home")
    assert resp.status_code == 200
    documents = resp.json()["documents"]
    assert documents["ar_file"]["filename"] == "ar.pdf"
    assert documents["kr_file"]["filename"] == "kr.pdf"
    assert documents["house_project_file"] is None

    body_text = resp.text
    assert "5000000" not in body_text
    assert "final_price" not in body_text
    assert "ivan@example.com" not in body_text
    assert "Тестово" not in body_text


def test_home_aktualnoe_reflects_this_cycle_stage(api, make_user, db):
    production = _make_production_with_client(db)
    db.commit()
    worker = api(make_user(Module.PRODUCTION))
    resp = worker.get(f"/api/production/{production.id}/home")
    aktualnoe = resp.json()["aktualnoe"]
    assert aktualnoe is not None
    assert aktualnoe["stage"] == "Производство дома"


def test_home_returns_404_for_unknown_production(api, make_user, db):
    worker = api(make_user(Module.PRODUCTION))
    resp = worker.get("/api/production/999999/home")
    assert resp.status_code == 404
