"""Удаление поставщика (0030-c):

- только администратор (`DELETE /api/warehouse/suppliers/{id}`);
- отказ 409 (не сырой IntegrityError), если у поставщика есть заказы
  (`app.accounting.SupplierOrder`);
- при отсутствии заказов поставщик удаляется вместе с прайсом/заметками
  (каскад на уровне БД).
"""

from app.accounting.models import SupplierOrder
from app.common.module_access import Module
from app.warehouse.models import Supplier, SupplierPriceItem


def _make_supplier(db, name="Поставщик"):
    supplier = Supplier(name=name)
    db.add(supplier)
    db.flush()
    return supplier


def test_delete_requires_admin(api, make_user, db):
    supplier = _make_supplier(db)
    db.commit()
    worker = api(make_user(Module.WAREHOUSE))
    resp = worker.delete(f"/api/warehouse/suppliers/{supplier.id}")
    assert resp.status_code == 403
    assert db.get(Supplier, supplier.id) is not None


def test_delete_rejected_with_supplier_orders(api, make_user, db):
    supplier = _make_supplier(db)
    db.add(SupplierOrder(supplier_id=supplier.id, items=[]))
    db.commit()
    admin = api(make_user(admin=True))
    resp = admin.delete(f"/api/warehouse/suppliers/{supplier.id}")
    assert resp.status_code == 409
    assert db.get(Supplier, supplier.id) is not None


def test_delete_succeeds_and_cascades_price_items(api, make_user, db):
    supplier = _make_supplier(db)
    db.add(SupplierPriceItem(supplier_id=supplier.id, material="Брус", tiers=[{"min_qty": 0, "price": 100}]))
    db.commit()
    supplier_id = supplier.id

    admin = api(make_user(admin=True))
    resp = admin.delete(f"/api/warehouse/suppliers/{supplier_id}")
    assert resp.status_code == 204
    assert db.get(Supplier, supplier_id) is None
