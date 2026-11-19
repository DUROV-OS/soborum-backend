from collections import defaultdict
from decimal import Decimal, InvalidOperation
import re

from app.moysklad.importer import ref
from app.moysklad.models import SourceRecord


def invoices_by_order(db, orders):
    """Read issued supplier invoices linked by the source order ID."""
    linked_orders = {order.source_id: order for order in orders if order.source_id}
    if not linked_orders:
        return {}

    currencies = {
        row.source_id: row.payload.get('isoCode')
        for row in db.query(SourceRecord).filter_by(entity_type='currency')
    }
    invoices = defaultdict(list)
    for row in db.query(SourceRecord).filter_by(entity_type='invoicein', archived=False).order_by(SourceRecord.id):
        payload = row.payload
        link = payload.get('purchaseOrder')
        if not isinstance(link, dict) or payload.get('applicable') is not True:
            continue
        meta = link.get('meta') or {}
        if not isinstance(meta, dict) or meta.get('type', 'purchaseorder') != 'purchaseorder':
            continue
        link = {**link, 'meta': meta}
        order = linked_orders.get(ref(link))
        if order is None:
            continue
        try:
            raw_amount = Decimal(str(payload.get('sum')))
            if not raw_amount.is_finite():
                continue
            amount = (raw_amount / 100).quantize(Decimal('0.01'))
        except InvalidOperation:
            continue

        currency_id = ref((payload.get('rate') or {}).get('currency'))
        currency = currencies.get(currency_id) if currency_id else order.currency
        currency = str(currency or '').upper()
        invoices[order.id].append({
            'id': row.id,
            'number': payload.get('incomingNumber') or row.title,
            'date': payload.get('incomingDate') or payload.get('moment'),
            'amount': format(amount, 'f'),
            'currency': currency if re.fullmatch('[A-Z]{3}', currency) else None,
        })
    return invoices


def totals(invoices):
    sums = defaultdict(Decimal)
    for invoice in invoices:
        if invoice['currency']:
            sums[invoice['currency']] += Decimal(invoice['amount'])
    return [{'currency': currency, 'amount': format(amount, 'f')} for currency, amount in sorted(sums.items())]
