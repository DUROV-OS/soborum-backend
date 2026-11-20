"""Read confirmed links between saved purchasing documents."""

from collections import defaultdict
import re

from sqlalchemy import tuple_

from app.accounting.models import MoneyMovement
from app.moysklad.importer import ref, rows
from app.moysklad.models import SourceLink, SourceRecord
from app.moysklad.supplier_invoices import amount


PAYMENT_TYPES = {'paymentout', 'cashout'}


def document_key(value, default_type=''):
    if not isinstance(value, dict):
        return None
    meta = value.get('meta') or {}
    identity = ref({**value, 'meta': meta})
    return (str(meta.get('type') or default_type).lower(), identity) if identity else None


def linked_source_documents(db, order, receipts, invoices, movement_ids, *, include_order=True):
    receipt_ids = {row['source_id']: row['id'] for row in receipts if row['source_id']}
    invoice_ids = {row['id']: row for row in invoices}
    order_source = order.source_id if order is not None and include_order else None
    if not order_source and not receipt_ids and not invoice_ids:
        return [], []

    source_keys = {('supply', identity) for identity in receipt_ids}
    if order_source:
        source_keys.add(('purchaseorder', order_source))
    sources = db.query(SourceRecord).filter(
        tuple_(SourceRecord.entity_type, SourceRecord.source_id).in_(source_keys),
    ).all()
    if invoice_ids:
        sources.extend(db.query(SourceRecord).filter(
            SourceRecord.entity_type == 'invoicein', SourceRecord.id.in_(invoice_ids),
        ))
    sources_by_key = {(row.entity_type, row.source_id): row for row in sources}

    factures_by_source = defaultdict(set)
    for identity, native_id in receipt_ids.items():
        supply = sources_by_key.get(('supply', identity))
        facture_key = document_key(supply.payload.get('factureIn'), 'facturein') if supply else None
        if facture_key and facture_key[0] == 'facturein':
            factures_by_source[facture_key[1]].add(native_id)

    facture_records = []
    if receipt_ids:
        for row in db.query(SourceRecord).filter_by(entity_type='facturein').order_by(SourceRecord.id):
            for link in rows(row.payload.get('supplies')):
                key = document_key(link, 'supply')
                if key and key[0] == 'supply' and key[1] in receipt_ids:
                    factures_by_source[row.source_id].add(receipt_ids[key[1]])
            if factures_by_source.get(row.source_id):
                facture_records.append(row)
                sources_by_key[('facturein', row.source_id)] = row

    payment_refs = defaultdict(set)
    for source_key, row in sources_by_key.items():
        for link in rows(row.payload.get('payments')):
            key = document_key(link, 'paymentout')
            if key and key[0] in PAYMENT_TYPES:
                payment_refs[key].add(source_key)

    payment_records = []
    for row in db.query(SourceRecord).filter(SourceRecord.entity_type.in_(PAYMENT_TYPES)).order_by(SourceRecord.id):
        key = (row.entity_type, row.source_id)
        linked = payment_refs.get(key, set()) | {
            target for operation in rows(row.payload.get('operations'))
            if (target := document_key(operation)) in sources_by_key
        }
        facture = document_key(row.payload.get('factureIn'), 'facturein')
        if facture in sources_by_key:
            linked.add(facture)
        if linked:
            payment_refs[key] = linked
            payment_records.append(row)

    records = [*facture_records, *payment_records]
    currency_ids = {ref((row.payload.get('rate') or {}).get('currency')) for row in records}
    currencies = {row.source_id: row.payload.get('isoCode') for row in db.query(SourceRecord).filter(
        SourceRecord.entity_type == 'currency', SourceRecord.source_id.in_(currency_ids - {''}),
    )} if currency_ids - {''} else {}
    def summary(row):
        raw = row.payload
        currency_id = ref((raw.get('rate') or {}).get('currency'))
        currency = str(currencies.get(currency_id) or raw.get('_currency_code')
                       or (order.currency if order and not currency_id else '') or '').upper()
        total = amount(raw, 'sum')
        return {'number': raw.get('name') or row.title, 'date': raw.get('moment'),
                'amount': str(total) if total is not None else None,
                'currency': currency if re.fullmatch('[A-Z]{3}', currency) else None}

    payment_keys = {row.entity_type + '/' + row.source_id for row in payment_records}
    mapped_movements = dict(db.query(SourceLink.source_key, MoneyMovement.id)
                            .join(MoneyMovement, MoneyMovement.id == SourceLink.target_id)
                            .filter(SourceLink.target_type == 'money_movements',
                                    SourceLink.source_key.in_(payment_keys)).all()) if payment_keys else {}
    factures = [
        {**summary(row), 'id': row.id, 'receipt_ids': sorted(factures_by_source[row.source_id]),
         'status': row.state}
        for row in facture_records
    ]
    payments = []
    for row in payment_records:
        identity = mapped_movements.get(row.entity_type + '/' + row.source_id)
        if identity in movement_ids:
            continue
        related = payment_refs[(row.entity_type, row.source_id)]
        payments.append({
            **summary(row), 'id': identity, 'source_record_id': row.id, 'source_type': row.entity_type,
            'status': 'posted' if row.payload.get('applicable') is True else 'draft',
            'refund': False,
            'invoice_ids': sorted(sources_by_key[k].id for k in related if k[0] == 'invoicein'),
        })

    for row in sources:
        if row.entity_type == 'invoicein':
            total = amount(row.payload, 'sum')
            paid = amount(row.payload, 'payedSum')
            label = 'Выставлен'
            if total and paid:
                label = 'Оплачен' if paid >= total else 'Оплачен частично'
            invoice_ids[row.id]['status'] = row.state or label
    return factures, payments
