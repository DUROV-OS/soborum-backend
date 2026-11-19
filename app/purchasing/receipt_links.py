"""Resolve posted receipt evidence linked through a supplier invoice in MoySklad."""

from collections import defaultdict

from app.moysklad.importer import ref, rows
from app.moysklad.models import SourceRecord
from app.purchasing.models import PurchaseReceipt


def invoice_receipts(db, orders):
    source_orders = {order.source_id: order for order in orders if order.source_id}
    if not source_orders:
        return {}

    candidates = defaultdict(set)
    for record in db.query(SourceRecord.payload).filter_by(entity_type='invoicein', archived=False):
        payload = record.payload
        order_id = ref(payload.get('purchaseOrder'))
        if not order_id or payload.get('applicable') is not True:
            continue
        supplies = payload.get('supplies') or []
        for item in rows(supplies):
            source_id = ref(item)
            if source_id:
                candidates[source_id].add(order_id)
    linked = {
        receipt_id: source_orders[order_id]
        for receipt_id, order_ids in candidates.items()
        if len(order_ids) == 1
        for order_id in order_ids
        if order_id in source_orders
    }
    if not linked:
        return {}

    receipts = defaultdict(list)
    for receipt in db.query(PurchaseReceipt).filter(
        PurchaseReceipt.source_id.in_(list(linked)),
        PurchaseReceipt.order_id.is_(None),
        PurchaseReceipt.status == 'posted',
    ):
        order = linked[receipt.source_id]
        if receipt.supplier_id == order.supplier_id:
            receipts[order.id].append(receipt)
    return receipts
