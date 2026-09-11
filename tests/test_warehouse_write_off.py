"""Списание материалов со склада (0030-e):

- доступно ADMIN и сотруднику с доступом к модулю WAREHOUSE, никому другому;
- причина обязательна;
- нельзя увести остаток в минус;
- списание уменьшает остаток и попадает в историю движений с причиной.
"""

from app.common.module_access import Module
from app.warehouse.models import MaterialCategory, StockMovementReason, Warehouse, WarehouseMaterial


def _make_material(db, quantity_in_stock=10):
    material = WarehouseMaterial(
        warehouse=Warehouse.TECHNOLOGY,
        category=MaterialCategory.NONE,
        title="Брус",
        code=f"M-{db.query(WarehouseMaterial).count()}",
        unit="шт",
        quantity_in_stock=quantity_in_stock,
    )
    db.add(material)
    db.flush()
    return material


def test_write_off_requires_admin_or_warehouse_access(api, make_user, db):
    material = _make_material(db)
    db.commit()
    outsider = api(make_user(Module.CLIENTS))
    resp = outsider.post(f"/api/warehouse/materials/{material.id}/write-off", json={"quantity": 1, "reason": "брак"})
    assert resp.status_code == 403
    assert float(db.get(WarehouseMaterial, material.id).quantity_in_stock) == 10


def test_write_off_by_admin(api, make_user, db):
    material = _make_material(db)
    db.commit()
    admin = api(make_user(admin=True))
    resp = admin.post(f"/api/warehouse/materials/{material.id}/write-off", json={"quantity": 3, "reason": "брак"})
    assert resp.status_code == 200
    assert resp.json()["quantity_in_stock"] == 7


def test_write_off_by_warehouse_worker(api, make_user, db):
    material = _make_material(db)
    db.commit()
    worker = api(make_user(Module.WAREHOUSE))
    resp = worker.post(
        f"/api/warehouse/materials/{material.id}/write-off",
        json={"quantity": 2, "reason": "недостача при инвентаризации"},
    )
    assert resp.status_code == 200
    assert resp.json()["quantity_in_stock"] == 8

    history = worker.get(f"/api/warehouse/materials/{material.id}/history").json()
    assert len(history) == 1
    assert history[0]["reason"] == "write_off"
    assert history[0]["delta"] == -2
    assert history[0]["note"] == "недостача при инвентаризации"


def test_write_off_requires_reason(api, make_user, db):
    material = _make_material(db)
    db.commit()
    admin = api(make_user(admin=True))
    resp = admin.post(f"/api/warehouse/materials/{material.id}/write-off", json={"quantity": 1, "reason": "  "})
    assert resp.status_code == 400
    assert float(db.get(WarehouseMaterial, material.id).quantity_in_stock) == 10


def test_write_off_cannot_exceed_stock(api, make_user, db):
    material = _make_material(db, quantity_in_stock=5)
    db.commit()
    admin = api(make_user(admin=True))
    resp = admin.post(f"/api/warehouse/materials/{material.id}/write-off", json={"quantity": 6, "reason": "брак"})
    assert resp.status_code == 400
    assert float(db.get(WarehouseMaterial, material.id).quantity_in_stock) == 5
