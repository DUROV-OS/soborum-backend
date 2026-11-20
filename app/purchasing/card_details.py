"""Positions and evidenced links for individual supplier invoices and receipts."""

from decimal import Decimal, InvalidOperation
import re

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import tuple_
from sqlalchemy.orm import Session

from app.accounting.models import MoneyMovement, SupplierOrder
from app.common.module_access import Module
from app.core.deps import require_view
from app.db.session import get_db
from app.moysklad.importer import ref, rows
from app.moysklad.models import SourceRecord
from app.moysklad.supplier_invoices import amount, invoice_columns
from app.purchasing.document_links import document_key, linked_source_documents
from app.purchasing.models import PurchaseReceipt, PurchaseReturn
from app.purchasing.receipt_register import register_items
from app.purchasing.returns import receipt

router = APIRouter()
view = require_view(Module.ACCOUNTING)


def invoice_record(db, id):
    row = db.get(SourceRecord, id)
    if row is None or row.entity_type != 'invoicein':
        raise HTTPException(404, 'Счёт поставщика не найден')
    return row


def source_receipt(db, row):
    if row.source_id:
        return db.query(SourceRecord).filter_by(entity_type='supply', source_id=row.source_id).first()
    return None


def reference_ids(payload, field, kind):
    return {key[1] for item in rows(payload.get(field))
            if (key := document_key(item, kind)) and key[0] == kind}


def receipt_invoice_ids(payload):
    ids = reference_ids(payload, 'invoicesIn', 'invoicein')
    key = document_key(payload.get('invoiceIn'), 'invoicein')
    if key and key[0] == 'invoicein':
        ids.add(key[1])
    return ids


def source_order_ids(invoices):
    return {key[1] for invoice in invoices
            if (key := document_key(invoice.payload.get('purchaseOrder'), 'purchaseorder'))
            and key[0] == 'purchaseorder'}


def order_for_invoices(db, invoices, supplier_id=None):
    ids = source_order_ids(invoices)
    if len(ids) == 1:
        query = db.query(SupplierOrder).filter_by(source_id=next(iter(ids)))
        if supplier_id is not None:
            query = query.filter_by(supplier_id=supplier_id)
        return query.first()
    return None


def decimal_or_none(value):
    try:
        parsed = Decimal(str(value))
        return parsed if parsed.is_finite() else None
    except InvalidOperation:
        return None


def positions(db, payload):
    positions = [line for line in rows(payload.get('positions')) if isinstance(line, dict)]
    keys = {key for line in positions if (key := document_key(line.get('assortment')))}
    references = {(row.entity_type, row.source_id): row for row in db.query(SourceRecord).filter(
        tuple_(SourceRecord.entity_type, SourceRecord.source_id).in_(keys),
    )} if keys else {}
    unit_ids = {ref(row.payload.get('uom')) for row in references.values()} - {''}
    units = {row.source_id: row.title for row in db.query(SourceRecord).filter(
        SourceRecord.entity_type == 'uom', SourceRecord.source_id.in_(unit_ids),
    )} if unit_ids else {}

    result = []
    for number, line in enumerate(positions, 1):
        assortment = line.get('assortment') or {}
        product = references.get(document_key(assortment))
        uom = assortment.get('uom') or (product.payload.get('uom') if product else None) or {}
        quantity = decimal_or_none(line.get('quantity'))
        price = amount(line, 'price')
        total = amount(line, 'sum')
        discount = decimal_or_none(line.get('discount', 0))
        if total is None and quantity is not None and price is not None and discount is not None:
            total = (price * quantity * (1 - discount / 100)).quantize(Decimal('.01'))
        vat = 0 if line.get('vatEnabled') is False or payload.get('vatEnabled') is False else decimal_or_none(line.get('vat'))
        result.append({
            'number': number, 'name': assortment.get('name') or (product.title if product else None)
            or line.get('name') or 'Наименование не сохранено',
            'quantity': float(quantity) if quantity is not None else None,
            'unit': uom.get('name') or units.get(ref(uom)),
            'vat': float(vat) if vat is not None else None, 'price': str(price) if price is not None else None,
            'amount': str(total) if total is not None else None,
        })
    return result


@router.get('/supplier-invoices/{id}')
def invoice_card(id: int, db: Session = Depends(get_db), user=Depends(view)):
    row = invoice_record(db, id)
    data, _, _ = invoice_columns(db, user, [row])
    return {**data[id], 'date': row.payload.get('moment'), 'archived': row.archived,
            'positions': positions(db, row.payload)}


def summary(kind, id, number, date, amount, currency, status):
    return {'kind': kind, 'id': id, 'number': number, 'date': date,
            'amount': amount, 'currency': currency, 'status': status}


def linked_cards(db, order, invoices, receipts, *, include_invoices, include_receipts):
    result = []
    if order:
        result.append(summary('order', order.id, order.number, order.created_at,
                              order.total_cost, order.currency, order.source_state))
    invoice_summaries = []
    invoice_currencies = {row.source_id: row.payload.get('isoCode') for row in db.query(SourceRecord).filter_by(
        entity_type='currency',
    )} if invoices else {}
    for invoice in invoices:
        raw = invoice.payload
        currency_id = ref((raw.get('rate') or {}).get('currency'))
        currency = str(invoice_currencies.get(currency_id) or raw.get('_currency_code')
                       or (order.currency if order and not currency_id else '') or '').upper()
        total = amount(raw, 'sum')
        invoice_summaries.append(summary(
            'invoice', invoice.id, raw.get('name') or invoice.title, raw.get('moment'),
            str(total) if total is not None else None,
            currency if re.fullmatch('[A-Z]{3}', currency) else None, invoice.state,
        ))
    receipt_rows = register_items(db, receipts)
    factures, payments = linked_source_documents(db, order, receipt_rows, invoice_summaries, set(), include_order=False)
    if include_invoices:
        result.extend(invoice_summaries)
    if include_receipts:
        result.extend(summary('receipt', row['id'], row['number'], row['occurred_at'],
                              row['amount'], row['currency'], row['status_label'] or (
                                  'Проведена' if row['status'] == 'posted' else 'Черновик'
                              )) for row in receipt_rows)
    result.extend({**facture, 'kind': 'facture'} for facture in factures)
    movement_ids = {item['id'] for item in payments if item['id'] is not None}
    movements = {move.id: move for move in db.query(MoneyMovement).filter(MoneyMovement.id.in_(movement_ids))} if movement_ids else {}
    for item in payments:
        move = movements.get(item['id'])
        if move:
            payment = movement_card(move)
        else:
            payment = summary('payment', item['source_record_id'], item['number'], item['date'],
                              item['amount'], item['currency'], 'Проведён' if item['status'] == 'posted' else 'Черновик')
            payment['source_type'] = item['source_type']
        result.append(payment)
    return result


def movement_card(move, kind='payment'):
    labels = {'draft': 'На согласовании', 'approved': 'Согласован', 'posted': 'Проведён', 'cancelled': 'Отменён'}
    return summary(kind, move.id, move.external_number or str(move.id), move.doc_date or move.created_at,
                   move.amount, move.currency, labels.get(move.status.value))


@router.get('/supplier-invoices/{id}/documents')
def invoice_documents(id: int, db: Session = Depends(get_db), user=Depends(view)):
    invoice = invoice_record(db, id)
    order = order_for_invoices(db, [invoice])
    ids = reference_ids(invoice.payload, 'supplies', 'supply')
    for source_id, payload in db.query(SourceRecord.source_id, SourceRecord.payload).filter_by(entity_type='supply'):
        if invoice.source_id in receipt_invoice_ids(payload):
            ids.add(source_id)
    receipts = db.query(PurchaseReceipt).filter(PurchaseReceipt.source_id.in_(ids)).order_by(PurchaseReceipt.id).all() if ids else []
    return linked_cards(db, order, [invoice], receipts, include_invoices=False, include_receipts=True)


@router.get('/receipts/{id}/documents')
def receipt_documents(id: int, db: Session = Depends(get_db), user=Depends(view)):
    row = receipt(db, id)
    original = source_receipt(db, row)
    direct = receipt_invoice_ids(original.payload) if original else set()
    invoices = [
        source for source in db.query(SourceRecord).filter_by(entity_type='invoicein').order_by(SourceRecord.id)
        if source.source_id in direct or row.source_id in reference_ids(source.payload, 'supplies', 'supply')
    ] if row.source_id else []
    order = db.get(SupplierOrder, row.order_id) if row.order_id is not None else order_for_invoices(db, invoices, row.supplier_id)
    result = linked_cards(db, order, invoices, [row], include_invoices=True, include_receipts=False)
    returns = db.query(PurchaseReturn).filter_by(receipt_id=id).order_by(PurchaseReturn.id).all()
    result.extend(summary('return', entry.id, entry.number, entry.occurred_at, entry.amount, entry.currency,
                          'Отменён' if entry.cancelled else 'Проведён') for entry in returns)
    if returns:
        refunds = db.query(MoneyMovement).filter(MoneyMovement.purchase_return_id.in_([entry.id for entry in returns]))
        result.extend(movement_card(move, 'refund') for move in refunds.order_by(MoneyMovement.id))
    return result
