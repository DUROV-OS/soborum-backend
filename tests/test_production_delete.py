"""Удаление производства и модуля дома (0030-b):

- только администратор;
- производство: отказ 409, если цикл не завершён или есть незавершённая
  заявка на материалы / незавершённая задача по модулю;
- модуль: отказ 409, если по нему уже выдавались материалы (started work) или
  есть незавершённая заявка/задача;
- при отсутствии зависимостей — удаление проходит (каскад на модули/материалы/
  заявки на уровне БД).
"""

from app.common.module_access import Module
from app.cycle.models import Cycle, CycleStatus
from app.production import service as production_service
from app.production.models import MaterialRequest, MaterialRequestStatus, ModuleMaterial, Production, ProductionModule
from app.tasks.models import Task, TaskLinkType, TaskStatus
from app.warehouse.models import MaterialCategory, Warehouse, WarehouseMaterial


def _make_production(db, cycle_status=CycleStatus.COMPLETED):
    cycle = Cycle(status=cycle_status)
    db.add(cycle)
    db.flush()
    production = Production(cycle_id=cycle.id, house_index=1, name="Дом")
    db.add(production)
    db.flush()
    return production


def _make_module(db, production):
    module = ProductionModule(production_id=production.id, name="Модуль")
    db.add(module)
    db.flush()
    return module


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


def _make_module_material(db, module, warehouse_material, quantity_provided=0):
    material = ModuleMaterial(
        module_id=module.id,
        warehouse_material_id=warehouse_material.id,
        inventory_number="INV-1",
        unit="шт",
        quantity_required=10,
        quantity_provided=quantity_provided,
    )
    db.add(material)
    db.flush()
    return material


def test_delete_production_requires_admin(api, make_user, db):
    production = _make_production(db)
    db.commit()
    worker = api(make_user(Module.PRODUCTION))
    resp = worker.delete(f"/api/production/{production.id}")
    assert resp.status_code == 403


def test_delete_production_rejected_with_active_cycle(api, make_user, db):
    production = _make_production(db, cycle_status=CycleStatus.PRODUCTION)
    db.commit()
    admin = api(make_user(admin=True))
    resp = admin.delete(f"/api/production/{production.id}")
    assert resp.status_code == 409
    assert db.get(Production, production.id) is not None


def test_delete_production_rejected_with_pending_material_request(api, make_user, db):
    production = _make_production(db)
    module = _make_module(db, production)
    warehouse_material = _make_warehouse_material(db)
    module_material = _make_module_material(db, module, warehouse_material)
    requester = make_user(Module.PRODUCTION)
    db.add(
        MaterialRequest(
            module_material_id=module_material.id,
            warehouse_material_id=warehouse_material.id,
            quantity=5,
            status=MaterialRequestStatus.PENDING,
            requested_by_id=requester.id,
        )
    )
    db.commit()
    admin = api(make_user(admin=True))
    resp = admin.delete(f"/api/production/{production.id}")
    assert resp.status_code == 409
    assert db.get(Production, production.id) is not None


def test_delete_production_rejected_with_open_module_task(api, make_user, db):
    production = _make_production(db)
    module = _make_module(db, production)
    assignee = make_user(Module.PRODUCTION)
    task = Task(title="Задача по модулю", module_id=module.id, status=TaskStatus.READY)
    task.assignees = [assignee]
    db.add(task)
    db.commit()
    admin = api(make_user(admin=True))
    resp = admin.delete(f"/api/production/{production.id}")
    assert resp.status_code == 409
    assert db.get(Production, production.id) is not None


def test_delete_production_succeeds_and_cascades(api, make_user, db):
    production = _make_production(db)
    module = _make_module(db, production)
    warehouse_material = _make_warehouse_material(db)
    module_material = _make_module_material(db, module, warehouse_material)
    assignee = make_user(Module.PRODUCTION)
    done_task = Task(title="Готовая задача", module_id=module.id, status=TaskStatus.DONE)
    done_task.assignees = [assignee]
    db.add(done_task)
    db.commit()
    production_id, module_id, material_id, task_id = production.id, module.id, module_material.id, done_task.id

    admin = api(make_user(admin=True))
    resp = admin.delete(f"/api/production/{production_id}")
    assert resp.status_code == 204
    assert db.get(Production, production_id) is None
    assert db.get(ProductionModule, module_id) is None
    assert db.get(ModuleMaterial, material_id) is None
    # Завершённая задача остаётся, но теряет ссылку на удалённый модуль.
    remaining_task = db.get(Task, task_id)
    assert remaining_task is not None
    assert remaining_task.module_id is None


def test_delete_module_rejected_when_material_issued(api, make_user, db):
    production = _make_production(db)
    module = _make_module(db, production)
    warehouse_material = _make_warehouse_material(db)
    _make_module_material(db, module, warehouse_material, quantity_provided=3)
    db.commit()
    admin = api(make_user(admin=True))
    resp = admin.delete(f"/api/production/modules/{module.id}")
    assert resp.status_code == 409
    assert db.get(ProductionModule, module.id) is not None


def test_delete_module_succeeds_without_issued_materials(api, make_user, db):
    production = _make_production(db)
    module = _make_module(db, production)
    warehouse_material = _make_warehouse_material(db)
    module_material = _make_module_material(db, module, warehouse_material, quantity_provided=0)
    db.commit()
    module_id, material_id = module.id, module_material.id

    admin = api(make_user(admin=True))
    resp = admin.delete(f"/api/production/modules/{module_id}")
    assert resp.status_code == 204
    assert db.get(ProductionModule, module_id) is None
    assert db.get(ModuleMaterial, material_id) is None
