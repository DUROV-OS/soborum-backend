"""Document-backed columns for the working receipt register."""

import re
from collections import defaultdict
from decimal import Decimal

from sqlalchemy import tuple_
from sqlalchemy.orm import joinedload

from app.accounting.models import Counterparty, SupplierOrder
from app.moysklad.importer import ref
from app.moysklad.models import SourceLink, SourceRecord
from app.moysklad.supplier_invoices import amount as source_amount, planned_day
from app.purchasing.control_models import PurchaseControlRecord
from app.purchasing.service import dec, receipt_out
from app.warehouse.models import Supplier, WarehouseMaterial


def register_items(db, receipts):
    if not receipts:
        return []

    source_ids = {row.source_id for row in receipts if row.source_id}
    sources = {
        row.source_id: row for row in db.query(SourceRecord).filter(
            SourceRecord.entity_type == 'supply', SourceRecord.source_id.in_(source_ids),
        )
    } if source_ids else {}
    reference_keys = {
        (kind, ref(value))
        for source in sources.values()
        for kind, value in (
            ('store', source.payload.get('store')),
            ('organization', source.payload.get('organization')),
            ('currency', (source.payload.get('rate') or {}).get('currency')),
        )
        if ref(value)
    }
    references = {
        (record.entity_type, record.source_id): record
        for record in db.query(SourceRecord).filter(
            tuple_(SourceRecord.entity_type, SourceRecord.source_id).in_(reference_keys),
        )
    } if reference_keys else {}

    supplier_ids = {row.supplier_id for row in receipts if row.supplier_id is not None}
    suppliers = dict(db.query(Supplier.id, Supplier.name).filter(Supplier.id.in_(supplier_ids)).all())
    by_supplier = defaultdict(list)
    for identity, supplier_id in db.query(Counterparty.id, Counterparty.supplier_id).filter(
        Counterparty.supplier_id.in_(supplier_ids),
    ):
        by_supplier[supplier_id].append(identity)
    agent_keys = {'counterparty/' + ref(source.payload.get('agent')) for source in sources.values()}
    by_source = dict(db.query(SourceLink.source_key, Counterparty.id)
                     .join(Counterparty, Counterparty.id == SourceLink.target_id)
                     .filter(SourceLink.target_type == 'counterparties', SourceLink.source_key.in_(agent_keys)).all())

    orders = {
        order.id: order for order in db.query(SupplierOrder).options(joinedload(SupplierOrder.organization)).filter(
            SupplierOrder.id.in_({row.order_id for row in receipts if row.order_id is not None}),
        )
    }
    material_ids = {line['material_id'] for row in receipts for line in row.lines if line.get('material_id')}
    warehouses = {
        identity: warehouse.value for identity, warehouse
        in db.query(WarehouseMaterial.id, WarehouseMaterial.warehouse).filter(WarehouseMaterial.id.in_(material_ids))
    }
    # Keep the original total (including source rounding) until an evidenced edit changes line values.
    corrected = {
        edit['receipt_id'] for (edit,) in db.query(PurchaseControlRecord.content).filter(
            PurchaseControlRecord.kind == 'receipt_edit',
            PurchaseControlRecord.content['receipt_id'].as_integer().in_(
                [row.id for row in receipts if row.source_id in sources],
            ),
        )
        if edit.get('before', {}).get('lines') != edit.get('after', {}).get('lines')
    } if sources else set()

    def reference_name(value, kind):
        record = references.get((kind, ref(value)))
        return (value or {}).get('name') or (record.title if record else None)

    items = []
    for row in receipts:
        source = sources.get(row.source_id)
        payload = source.payload if source else {}
        order = orders.get(row.order_id)
        counterparty_id = by_source.get('counterparty/' + ref(payload.get('agent')))
        candidates = by_supplier[row.supplier_id]
        if counterparty_id is None and len(candidates) == 1:
            counterparty_id = candidates[0]
        warehouse = reference_name(payload.get('store'), 'store')
        if not warehouse:
            warehouse = ', '.join(sorted({warehouses[line['material_id']] for line in row.lines
                                          if line.get('material_id') in warehouses})) or None
        organization = reference_name(payload.get('organization'), 'organization')
        if not organization and order and order.organization:
            organization = order.organization.short_name
        currency_record = references.get(('currency', ref((payload.get('rate') or {}).get('currency'))))
        currency = str((currency_record.payload.get('isoCode') if currency_record else None)
                       or payload.get('_currency_code') or (order.currency if order else '')
                       or ('RUB' if not row.source_id else '')).upper()
        total = source_amount(payload, 'sum') if row.id not in corrected else None
        if total is None:
            total = sum((dec(line['quantity']) * dec(line['unit_price']) for line in row.lines),
                        Decimal(0)).quantize(Decimal('.01'))
        paid = source_amount(payload, 'payedSum')
        incoming_date = planned_day(payload.get('incomingDate'))
        items.append({
            **receipt_out(row),
            'counterparty_id': counterparty_id,
            'supplier_name': suppliers.get(row.supplier_id),
            'warehouse': warehouse,
            'organization_name': organization,
            'currency': currency if re.fullmatch('[A-Z]{3}', currency) else None,
            'amount': str(total),
            'paid': str(paid) if paid is not None else None,
            'incoming_date': incoming_date.isoformat() if incoming_date else None,
            'incoming_number': payload.get('incomingNumber'),
            'status_label': source.state if source else None,
        })
    return items
