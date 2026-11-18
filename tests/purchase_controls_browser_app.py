"""Synthetic purchasing server; inherited bootstrap requires COSTING_BROWSER_TEST=1."""
import tests.costing_browser_app as t
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from sqlalchemy.orm import Session
from app.purchasing.router import app as purchasing
from app.purchasing.models import PurchaseReceipt
from app.accounting.models import SupplierOrder, MoneyMovement
from app.warehouse.models import Supplier, WarehouseMaterial, StockMovement

purchasing.dependency_overrides.update(t.app.dependency_overrides)
t.app.mount('/api/purchasing', purchasing)
t.reset()
ids = t.seed_native()
with Session(t.engine) as db:
    raw = db.query(WarehouseMaterial).filter_by(code='LINK-RAW').one()
    supplier = Supplier(name='Проверяемый поставщик'); competitor = Supplier(name='Другой поставщик')
    db.add_all([supplier, competitor]); db.flush()
    items = [{'material': raw.title, 'warehouse_material_id': raw.id, 'unit': raw.unit, 'quantity': 4,
              'unit_price': 50, 'stock_tracked': True, 'received_quantity': 0}]
    previous = SupplierOrder(supplier_id=supplier.id, number='ПРЕДЫДУЩИЙ-01', items=deepcopy(items), total_cost=200)
    current = SupplierOrder(supplier_id=supplier.id, number='ТЕКУЩИЙ-02', items=deepcopy(items), total_cost=200)
    db.add_all([previous, current]); db.flush()
    receipt = PurchaseReceipt(order_id=previous.id, supplier_id=supplier.id, actor_id=1, number='ПРИЁМКА-01',
        occurred_at=datetime.now(timezone.utc)-timedelta(days=1), status='posted',
        lines=[{'order_line': 0, 'material_id': raw.id, 'title': raw.title, 'unit': raw.unit, 'quantity': 4, 'unit_price': 50}])
    db.add(receipt); db.commit()
    ids.update(order_id=current.id, previous_id=previous.id, supplier_id=supplier.id, competitor_id=competitor.id,
               receipt_id=receipt.id, material_id=raw.id)


@t.app.get('/api/purchase-test/data')
def data():
    return ids


@t.app.get('/api/purchase-test/counts')
def counts():
    with Session(t.engine) as db:
        return {'stock_movements': db.query(StockMovement).count(), 'payments': db.query(MoneyMovement).count()}


@t.app.post('/api/purchase-test/change-price')
def change():
    with Session(t.engine) as db:
        row = db.get(SupplierOrder, ids['order_id']); row.items = [{**row.items[0], 'unit_price': 55}]
        row.version += 1; db.commit()
    return {'changed': True}


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(t.app, host='0.0.0.0', port=8133, log_level='warning')
