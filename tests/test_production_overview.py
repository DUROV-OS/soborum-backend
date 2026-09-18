"""Список «Производство» (0071): признак завершённости и критичность,
рассчитанные batch'ем по реальным блокам/задачам/заявкам на материалы, без
обращения к ИИ."""

from datetime import datetime, timedelta, timezone

from app.common.module_access import Module
from app.cycle.models import Cycle, CycleStatus
from app.production.models import BlockMaterial, MaterialRequest, MaterialRequestStatus, Production, ProductionBlock
from app.tasks.models import Task, TaskStatus
from app.warehouse.models import MaterialCategory, Warehouse, WarehouseMaterial


def _make_production(db, name="Дом"):
    cycle = Cycle(status=CycleStatus.PRODUCTION)
    db.add(cycle)
    db.flush()
    production = Production(cycle_id=cycle.id, house_index=1, name=name)
    db.add(production)
    db.flush()
    return production


def _make_block(db, production, name="Каркас"):
    block = ProductionBlock(production_id=production.id, name=name)
    db.add(block)
    db.flush()
    return block


def _make_warehouse_material(db, title="Брус"):
    material = WarehouseMaterial(
        warehouse=Warehouse.TECHNOLOGY, category=MaterialCategory.NONE, title=title,
        code=f"M-{db.query(WarehouseMaterial).count()}", unit="шт", quantity_in_stock=100,
    )
    db.add(material)
    db.flush()
    return material


def _row_by_id(body, production_id):
    return next(row for row in body if row["id"] == production_id)


def test_production_without_blocks_is_not_completed(api, make_user, db):
    production = _make_production(db, name="Без блоков")
    db.commit()
    worker = api(make_user(Module.PRODUCTION))

    resp = worker.get("/api/production/")
    assert resp.status_code == 200
    row = _row_by_id(resp.json(), production.id)
    assert row["is_completed"] is False
    assert row["criticality"] == "normal"


def test_all_blocks_done_marks_completed(api, make_user, db):
    production = _make_production(db, name="Готовый дом")
    block = _make_block(db, production)
    task = Task(title="Смонтировать стропила", block_id=block.id, status=TaskStatus.DONE)
    db.add(task)
    db.commit()
    worker = api(make_user(Module.PRODUCTION))

    resp = worker.get("/api/production/")
    row = _row_by_id(resp.json(), production.id)
    assert row["is_completed"] is True
    assert row["criticality"] == "normal"


def test_overdue_open_task_marks_critical(api, make_user, db):
    production = _make_production(db, name="Просрочка")
    block = _make_block(db, production, name="Кровля")
    task = Task(
        title="Смонтировать стропила", block_id=block.id, status=TaskStatus.READY,
        deadline=datetime.now(timezone.utc) - timedelta(days=3),
    )
    db.add(task)
    db.commit()
    worker = api(make_user(Module.PRODUCTION))

    resp = worker.get("/api/production/")
    row = _row_by_id(resp.json(), production.id)
    assert row["is_completed"] is False
    assert row["criticality"] == "critical"


def test_pending_material_request_marks_warning_when_not_completed(api, make_user, db):
    production = _make_production(db, name="Зависшая заявка")
    block = _make_block(db, production)
    requester = make_user(Module.PRODUCTION)
    warehouse_material = _make_warehouse_material(db)
    material = BlockMaterial(
        block_id=block.id, warehouse_material_id=warehouse_material.id,
        inventory_number="INV-1", unit="шт", quantity_required=0, quantity_requested=5,
    )
    db.add(material)
    db.flush()
    db.add(MaterialRequest(
        block_material_id=material.id, warehouse_material_id=warehouse_material.id,
        quantity=5, status=MaterialRequestStatus.PENDING, requested_by_id=requester.id,
    ))
    # Открытая задача блока без дедлайна — производство не завершено и не критично по срокам.
    task = Task(title="Открытая задача", block_id=block.id, status=TaskStatus.READY)
    db.add(task)
    db.commit()
    worker = api(requester)

    resp = worker.get("/api/production/")
    row = _row_by_id(resp.json(), production.id)
    assert row["is_completed"] is False
    assert row["criticality"] == "warning"


def test_completed_production_forces_normal_despite_pending_request(api, make_user, db):
    """Заявка на материал живёт своей задачей, не привязанной к блоку
    (`request_material` создаёт link-задачу склада) — поэтому у блока может
    не быть открытых задач (`is_completed=True`), а зависшая заявка всё
    равно существовать. Спека требует 'normal' на завершённых в любом случае."""
    production = _make_production(db, name="Завершён, но заявка висит")
    block = _make_block(db, production)
    requester = make_user(Module.PRODUCTION)
    warehouse_material = _make_warehouse_material(db)
    material = BlockMaterial(
        block_id=block.id, warehouse_material_id=warehouse_material.id,
        inventory_number="INV-1", unit="шт", quantity_required=0, quantity_requested=5,
    )
    db.add(material)
    db.flush()
    db.add(MaterialRequest(
        block_material_id=material.id, warehouse_material_id=warehouse_material.id,
        quantity=5, status=MaterialRequestStatus.PENDING, requested_by_id=requester.id,
    ))
    task = Task(title="Задача блока", block_id=block.id, status=TaskStatus.DONE)
    db.add(task)
    db.commit()
    worker = api(requester)

    resp = worker.get("/api/production/")
    row = _row_by_id(resp.json(), production.id)
    assert row["is_completed"] is True
    assert row["criticality"] == "normal"


def test_unrequested_shortfall_marks_warning_when_not_completed(api, make_user, db):
    production = _make_production(db, name="Недостача без заявки")
    block = _make_block(db, production)
    warehouse_material = _make_warehouse_material(db)
    material = BlockMaterial(
        block_id=block.id, warehouse_material_id=warehouse_material.id,
        inventory_number="INV-2", unit="шт", quantity_required=3, quantity_requested=0,
    )
    db.add(material)
    task = Task(title="Открытая задача", block_id=block.id, status=TaskStatus.READY)
    db.add(task)
    db.commit()
    worker = api(make_user(Module.PRODUCTION))

    resp = worker.get("/api/production/")
    row = _row_by_id(resp.json(), production.id)
    assert row["is_completed"] is False
    assert row["criticality"] == "warning"


def test_no_signals_and_open_task_is_normal(api, make_user, db):
    production = _make_production(db, name="Без сигналов")
    block = _make_block(db, production)
    task = Task(title="Открытая задача без дедлайна", block_id=block.id, status=TaskStatus.READY)
    db.add(task)
    db.commit()
    worker = api(make_user(Module.PRODUCTION))

    resp = worker.get("/api/production/")
    row = _row_by_id(resp.json(), production.id)
    assert row["is_completed"] is False
    assert row["criticality"] == "normal"
