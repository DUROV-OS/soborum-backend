"""Server-side procurement journal; filters and totals cover all matching rows."""
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo
from typing import Literal

from fastapi import HTTPException
from sqlalchemy import String, case, cast, func, or_, select
from sqlalchemy.orm import Session
from app.accounting.models import SupplierOrder, SupplierOrderStatus, MoneyMovement, MoneyMovementStatus
from app.purchasing.models import PurchaseReturn
from app.warehouse.models import Supplier

PaymentFilter = Literal['all', 'unpaid', 'partial', 'paid', 'overpaid', 'pending']
DeliveryFilter = Literal['all', 'ordered', 'in_transit', 'received', 'overdue']
SortOrder = Literal['newest', 'oldest', 'number', 'supplier', 'amount_desc', 'expected']
MOSCOW = ZoneInfo('Europe/Moscow')


def period(stmt, column, date_from, date_to):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, 'Начало периода не может быть позже окончания')
    if date_from:
        stmt = stmt.filter(column >= datetime.combine(date_from, time.min, MOSCOW))
    if date_to:
        stmt = stmt.filter(column < datetime.combine(date_to + timedelta(days=1), time.min, MOSCOW))
    return stmt


def search_term(value):
    return '%' + value.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'


def balances():
    paid_rows = (select(func.coalesce(func.sum(MoneyMovement.amount), 0))
                 .where(MoneyMovement.supply_id == SupplierOrder.id, MoneyMovement.status == MoneyMovementStatus.POSTED)
                 .correlate(SupplierOrder).scalar_subquery())
    pending = (select(func.coalesce(func.sum(MoneyMovement.amount), 0))
               .where(MoneyMovement.supply_id == SupplierOrder.id, MoneyMovement.status.in_([MoneyMovementStatus.DRAFT, MoneyMovementStatus.APPROVED]))
               .correlate(SupplierOrder).scalar_subquery())
    credit = (select(func.coalesce(func.sum(PurchaseReturn.amount), 0))
              .where(PurchaseReturn.order_id == SupplierOrder.id, PurchaseReturn.cancelled.is_(False))
              .correlate(SupplierOrder).scalar_subquery())
    refund = (select(func.coalesce(func.sum(MoneyMovement.amount), 0))
              .join(PurchaseReturn, MoneyMovement.purchase_return_id == PurchaseReturn.id)
              .where(PurchaseReturn.order_id == SupplierOrder.id, MoneyMovement.status == MoneyMovementStatus.POSTED)
              .correlate(SupplierOrder).scalar_subquery())
    paid = SupplierOrder.opening_paid + paid_rows - refund
    raw_net = SupplierOrder.total_cost - credit
    net = case((raw_net > 0, raw_net), else_=0)
    unpaid = case((net > paid, net - paid), else_=0)
    return paid, pending, net, unpaid


def orders(db: Session, *, query='', archive='active', supplier_id=None, payment='all', delivery='all', date_from=None, date_to=None, sort='newest', offset=0, limit=50):
    from app.purchasing.service import out
    paid, pending, net, unpaid = balances()
    stmt = db.query(SupplierOrder).join(Supplier)
    if archive != 'all':
        stmt = stmt.filter(SupplierOrder.archived.is_(archive == 'archived'))
    if supplier_id:
        stmt = stmt.filter(SupplierOrder.supplier_id == supplier_id)
    if query.strip():
        term = search_term(query.strip())
        stmt = stmt.filter(or_(*[col.ilike(term, escape='\\') for col in
            (SupplierOrder.number, Supplier.name, SupplierOrder.comment, cast(SupplierOrder.items, String))]))
    stmt = period(stmt, SupplierOrder.created_at, date_from, date_to)
    if delivery == 'overdue':
        stmt = stmt.filter(SupplierOrder.expected_at < datetime.now(MOSCOW).date(), SupplierOrder.status != SupplierOrderStatus.RECEIVED)
    elif delivery != 'all':
        stmt = stmt.filter(SupplierOrder.status == SupplierOrderStatus(delivery))
    condition = {
        'unpaid': (paid <= 0) & (net > 0), 'partial': (paid > 0) & (paid < net),
        'paid': paid >= net, 'overpaid': paid > net, 'pending': pending > 0,
    }.get(payment)
    if condition is not None:
        stmt = stmt.filter(condition)
    totals = stmt.with_entities(SupplierOrder.currency.label('currency'), SupplierOrder.total_cost.label('total'),
                               paid.label('paid'), pending.label('pending'), net.label('net'), unpaid.label('unpaid')).subquery()
    summary = [dict(currency=r.currency, total=float(r.total), paid=float(r.paid), pending=float(r.pending), net=float(r.net), unpaid=float(r.unpaid))
               for r in db.query(totals.c.currency, *[func.sum(totals.c[k]).label(k) for k in ('total', 'paid', 'pending', 'net', 'unpaid')])
               .group_by(totals.c.currency).order_by(totals.c.currency)]
    ordering = {
        'newest': SupplierOrder.created_at.desc(), 'oldest': SupplierOrder.created_at.asc(),
        'number': SupplierOrder.number.asc(), 'supplier': Supplier.name.asc(),
        'amount_desc': SupplierOrder.total_cost.desc(), 'expected': SupplierOrder.expected_at.asc().nullslast(),
    }[sort]
    return {'total': stmt.count(), 'summary': summary,
            'items': [out(db, row) for row in stmt.order_by(ordering, SupplierOrder.id.asc() if sort == 'oldest' else SupplierOrder.id.desc()).offset(offset).limit(limit)]}
