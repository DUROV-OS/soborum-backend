"""Source-backed supplier invoice columns and local workflow statuses."""

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.common.module_access import AccessLevel, Module
from app.core.deps import require_edit, require_view
from app.db.session import get_db
from app.moysklad.importer import ref
from app.moysklad.models import SourceLink, SourceRecord, SupplierInvoiceStatus, SupplierInvoiceStatusChoice

router = APIRouter()
view = require_view(Module.ACCOUNTING)
edit = require_edit(Module.ACCOUNTING)
BUILT_IN = {'paid': 'Оплачено', 'unpaid': 'Не оплачено', 'waiting': 'Ожидаем'}


def amount(payload, key):
    raw = payload.get(key)
    if raw is None:
        return None
    try:
        minor = Decimal(str(raw))
        return (minor / 100).quantize(Decimal('.01')) if minor.is_finite() else None
    except InvalidOperation:
        return None


def planned_day(value):
    try:
        return date.fromisoformat(value[:10]) if isinstance(value, str) else None
    except ValueError:
        return None


def status_out(row):
    return {'id': row.id, 'code': row.code, 'label': row.label, 'version': row.version}


def status_catalog(db):
    return [status_out(row) for row in db.query(SupplierInvoiceStatus).order_by(SupplierInvoiceStatus.id)]


def invoice_columns(db, user, records):
    catalog = status_catalog(db)
    statuses = {entry['id']: entry for entry in catalog}
    automatic_labels = {**BUILT_IN, **{entry['code']: entry['label'] for entry in catalog if entry['code']}}
    choices = {choice.invoice_id: choice for choice in db.query(SupplierInvoiceStatusChoice).filter(
        SupplierInvoiceStatusChoice.invoice_id.in_([record.id for record in records]),
    )} if records else {}
    references = {(r.entity_type, r.source_id): r for r in db.query(SourceRecord).filter(
        SourceRecord.entity_type.in_(('store', 'counterparty', 'currency')),
    )} if records else {}
    counterparties = {link.source_key: link.target_id for link in db.query(SourceLink).filter_by(target_type='counterparties')} if records else {}
    can_edit = user.access_level(Module.ACCOUNTING) >= AccessLevel.EDIT
    today = datetime.now(ZoneInfo('Europe/Moscow')).date()

    def source_name(payload, field, kind):
        value = payload.get(field) or {}
        source = references.get((kind, ref(value)))
        return value.get('name') or (source.title if source else None)

    columns = {}
    for record in records:
        payload = record.payload
        total, paid, accepted = (amount(payload, key) for key in ('sum', 'payedSum', 'shippedSum'))
        planned = planned_day(payload.get('paymentPlannedMoment'))
        if payload.get('applicable') is True and total is not None and total > 0 and paid is not None and paid >= total:
            auto = 'paid'
        elif planned and planned >= today:
            auto = 'waiting'
        else:
            auto = 'unpaid'
        choice = choices.get(record.id)
        override = statuses.get(choice.status_id) if choice else None
        code = 'paid' if auto == 'paid' else (override['code'] or 'custom') if override else auto
        label = override['label'] if override and auto != 'paid' else automatic_labels[auto]
        currency = references.get(('currency', ref((payload.get('rate') or {}).get('currency'))))
        currency_code = str((currency.payload.get('isoCode') if currency else None)
                            or payload.get('_currency_code') or '').upper()
        columns[record.id] = {
            'number': payload.get('name') or record.title, 'agent_name': source_name(payload, 'agent', 'counterparty'),
            'counterparty_id': counterparties.get('counterparty/' + ref(payload.get('agent'))),
            'warehouse': source_name(payload, 'store', 'store'),
            'currency': currency_code if re.fullmatch('[A-Z]{3}', currency_code) else None,
            'amount': str(total) if total is not None else None, 'paid': str(paid) if paid is not None else None,
            'accepted': str(accepted) if accepted is not None else None,
            'planned_payment_date': str(planned) if planned else None, 'incoming_number': payload.get('incomingNumber'),
            'incoming_date': payload.get('incomingDate'), 'comment': payload.get('description'),
            'status_code': code, 'status_label': label, 'automatic_label': automatic_labels[auto],
            'status_id': choice.status_id if choice else None, 'status_version': choice.version if choice else 0,
            'can_change_status': can_edit and not record.archived and auto != 'paid',
        }
    return columns, catalog, can_edit


class StatusLabel(BaseModel):
    model_config = ConfigDict(extra='forbid')
    label: str = Field(min_length=1, max_length=64)


class Rename(StatusLabel):
    version: int = Field(ge=1)


class StatusChoice(BaseModel):
    model_config = ConfigDict(extra='forbid')
    status_id: int | None = Field(gt=0)
    version: int = Field(ge=0)


def save_label(db, row, value):
    label = ' '.join(value.split())
    if not label:
        raise HTTPException(422, 'Укажите название статуса')
    row.label = label
    row.label_key = label.casefold()
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, 'Статус с таким названием уже есть')
    return status_out(row)


@router.post('/supplier-invoice-statuses', status_code=201)
def create_status(body: StatusLabel, db: Session = Depends(get_db), user=Depends(edit)):
    row = SupplierInvoiceStatus()
    db.add(row)
    return save_label(db, row, body.label)


@router.patch('/supplier-invoice-statuses/{id}')
def rename_status(id: int, body: Rename, db: Session = Depends(get_db), user=Depends(edit)):
    row = db.query(SupplierInvoiceStatus).filter_by(id=id).with_for_update().first()
    if row is None:
        raise HTTPException(404, 'Статус не найден')
    if row.version != body.version:
        raise HTTPException(409, 'Статус изменился. Обновите список.')
    row.version += 1
    return save_label(db, row, body.label)


@router.patch('/supplier-invoices/{id}/status')
def choose_status(id: int, body: StatusChoice, db: Session = Depends(get_db), user=Depends(edit)):
    record = db.query(SourceRecord).filter_by(id=id, entity_type='invoicein').with_for_update().first()
    if record is None:
        raise HTTPException(404, 'Счёт поставщика не найден')
    columns, _, _ = invoice_columns(db, user, [record])
    if not columns[id]['can_change_status']:
        raise HTTPException(409, 'Статус оплаченного или архивного счёта задаётся по документам.')
    if body.status_id is not None:
        status = db.get(SupplierInvoiceStatus, body.status_id)
        if status is None or status.code in {'paid', 'unpaid'}:
            raise HTTPException(422, 'Этот статус определяется по сумме оплаты.')
    row = db.get(SupplierInvoiceStatusChoice, id)
    if body.version != (row.version if row else 0):
        raise HTTPException(409, 'Статус счёта изменился. Обновите список.')
    if row is None:
        row = SupplierInvoiceStatusChoice(invoice_id=id, version=0)
        db.add(row)
    row.status_id = body.status_id
    row.updated_by_id = user.id
    row.version += 1
    db.commit()
    return invoice_columns(db, user, [record])[0][id]
