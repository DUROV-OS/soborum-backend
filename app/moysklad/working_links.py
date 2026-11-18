"""Navigate from source evidence to editable working records; never mutate the source."""
from app.common.module_access import Module
from app.moysklad.models import SourceLink
from app.warehouse.models import WarehouseMaterial
from app.accounting.models import SupplierOrder
from app.purchasing.models import PurchaseReceipt


def links(db, user, source):
    if source.entity_type in ('product', 'variant', 'bundle') and user.has_access(Module.WAREHOUSE):
        prefix = 'stock/' + source.source_id + '/'
        records = db.query(SourceLink).filter_by(target_type='warehouse_materials').all()
        ids = {r.target_id for r in records if r.source_key.startswith(prefix)}
        return [{'title': 'Рабочая карточка · ' + m.warehouse.value, 'url': f'/warehouse?tab=materials&material={m.id}'}
                for m in db.query(WarehouseMaterial).filter(WarehouseMaterial.id.in_(ids)).order_by(WarehouseMaterial.id)]
    if user.has_access(Module.ACCOUNTING):
        if source.entity_type == 'purchaseorder':
            row = db.query(SupplierOrder).filter_by(source_id=source.source_id).first()
            if row: return [{'title': 'Рабочий заказ поставщику', 'url': f'/purchasing?tab=orders&order={row.id}'}]
        if source.entity_type == 'supply':
            row = db.query(PurchaseReceipt).filter_by(source_id=source.source_id).first()
            if row: return [{'title': 'Рабочая приёмка', 'url': f'/purchasing?tab=receipts&receipt={row.id}'}]
    return []
