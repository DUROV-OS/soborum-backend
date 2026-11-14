from datetime import date
from app.purchasing import journal
from fastapi import FastAPI, Depends, Query, HTTPException
from sqlalchemy.orm import Session
from app.core.deps import require_view, require_edit, require_full
from app.common.module_access import Module
from app.db.session import get_db
from app.accounting.models import SupplierOrder, MoneyMovement
from app.accounting.schemas import (
    SupplierOrderCreate,
    SupplierOrderUpdate,
    MoneyMovementCreate,
    MoneyMovementOut,
)
from app.accounting import service as accounting
from app.purchasing import service
from app.purchasing.models import PurchaseReceipt
from app.purchasing.schemas import Receive, Archive, Payment
from app.users.models import User
from app.purchasing.returns import router as returns_router

app = FastAPI(title="Закупки и приёмки")
app.include_router(returns_router)
from app.purchasing.control_router import router as control_router
app.include_router(control_router)
view = require_view(Module.ACCOUNTING)
edit = require_edit(Module.ACCOUNTING)


@app.get("/catalog")
def catalog(db: Session = Depends(get_db), user: User = Depends(view)):
    from app.warehouse.models import Supplier, WarehouseMaterial
    from app.accounting.models import BankAccount

    return {
        "suppliers": [
            {"id": s.id, "name": s.name, "archived": s.status.value == "archived"}
            for s in db.query(Supplier).order_by(Supplier.name)
        ],
        "materials": (
            [
                {
                    "id": m.id,
                    "title": m.title,
                    "warehouse": m.warehouse.value,
                    "unit": m.unit,
                    "price": m.purchase_price,
                }
                for m in db.query(WarehouseMaterial)
                .filter_by(is_archived=False)
                .order_by(WarehouseMaterial.title)
            ]
            if user.has_access(Module.WAREHOUSE)
            else []
        ),
        "accounts": [
            {"id": a.id, "name": a.name + " · " + a.organization.short_name}
            for a in db.query(BankAccount).filter_by(is_active=True)
        ],
    }


@app.get("/orders")
def orders(
    query: str = "",
    archive: str = Query("active", pattern="^(active|archived|all)$"),
    supplier_id: int | None = Query(None, gt=0),
    payment: journal.PaymentFilter = 'all',
    delivery: journal.DeliveryFilter = 'all',
    date_from: date | None = None,
    date_to: date | None = None,
    sort: journal.SortOrder = 'newest',
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(view),
):
    return journal.orders(db, query=query, archive=archive, supplier_id=supplier_id, payment=payment,
                          delivery=delivery, date_from=date_from, date_to=date_to, sort=sort, offset=offset, limit=limit)



@app.get("/orders/{id}")
def order(id: int, db: Session = Depends(get_db), user: User = Depends(view)):
    return service.out(db, service.get_order(db, id))


@app.get("/orders/{id}/documents")
def order_documents(id: int, db: Session = Depends(get_db), user: User = Depends(view)):
    from app.purchasing.models import PurchaseReturn
    from sqlalchemy import or_
    row = service.get_order(db, id)
    receipts = db.query(PurchaseReceipt).filter_by(order_id=id).order_by(PurchaseReceipt.occurred_at, PurchaseReceipt.id).all()
    returns = db.query(PurchaseReturn).filter_by(order_id=id).order_by(PurchaseReturn.occurred_at, PurchaseReturn.id).all()
    return_ids = [r.id for r in returns]
    movements = db.query(MoneyMovement).filter(or_(MoneyMovement.supply_id == id, MoneyMovement.purchase_return_id.in_(return_ids))).order_by(MoneyMovement.created_at, MoneyMovement.id).all()
    return {
        'receipts': [service.receipt_out(r) for r in receipts],
        'returns': [{'id': r.id, 'number': r.number, 'receipt_id': r.receipt_id, 'occurred_at': r.occurred_at, 'amount': r.amount, 'currency': r.currency, 'cancelled': r.cancelled} for r in returns],
        'payments': [{'id': m.id, 'number': m.external_number, 'created_at': m.created_at, 'amount': m.amount, 'currency': m.currency, 'status': m.status.value, 'refund': m.purchase_return_id is not None} for m in movements],
        'opening_paid': row.opening_paid,
    }


@app.post("/orders", status_code=201)
def create(body: SupplierOrderCreate, db: Session = Depends(get_db), user: User = Depends(edit)):
    for item in body.items:
        if item.received_quantity:
            raise HTTPException(422, "Количество полученного заполняется приёмками")
    return service.out(db, accounting.create_supplier_order(db, body))


@app.patch("/orders/{id}")
def update(
    id: int,
    body: SupplierOrderUpdate,
    version: int,
    db: Session = Depends(get_db),
    user: User = Depends(edit),
):
    row = service.get_order(db, id, lock=True)
    if row.version != version or row.archived:
        raise HTTPException(409, "Заказ изменён или находится в архиве")
    if body.items is not None and any(service.dec(i.get("received_quantity")) for i in row.items):
        raise HTTPException(409, "После приёмки состав заказа не меняется. Оформите отдельный заказ.")
    if body.items is not None and any(i.received_quantity for i in body.items):
        raise HTTPException(422, "Количество полученного заполняется приёмками")
    return service.out(db, accounting.update_supplier_order(db, row, body))


@app.post("/orders/{id}/archive")
def archive(
    id: int,
    body: Archive,
    db: Session = Depends(get_db),
    user: User = Depends(require_full(Module.ACCOUNTING)),
):
    row = service.get_order(db, id, lock=True)
    if row.version != body.version:
        raise HTTPException(409, "Заказ изменён. Обновите карточку.")
    row.archived = body.archived
    row.version += 1
    db.commit()
    return service.out(db, row)


@app.post("/orders/{id}/payments", status_code=201)
def payment(id: int, body: Payment, db: Session = Depends(get_db), user: User = Depends(edit)):
    from app.accounting.models import MoneySubkind

    row = service.get_order(db, id)
    if row.archived:
        raise HTTPException(409, "Заказ находится в архиве")
    movement = accounting.create_money_movement(
        db,
        MoneyMovementCreate(
            subkind=MoneySubkind.SUPPLY_PAYMENT,
            amount=float(body.amount),
            account_id=body.account_id,
            supply_id=id,
        ),
        initiator_id=user.id,
        request_id=str(body.request_id),
    )
    return MoneyMovementOut.from_movement(movement)


@app.get("/receipts")
def receipts(
    order_id: int | None = None,
    query: str = '',
    supplier_id: int | None = Query(None, gt=0),
    date_from: date | None = None,
    date_to: date | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(view),
):
    stmt = db.query(PurchaseReceipt)
    if order_id is not None:
        stmt = stmt.filter_by(order_id=order_id)
    if supplier_id:
        stmt = stmt.filter(PurchaseReceipt.supplier_id == supplier_id)
    if query.strip():
        from app.warehouse.models import Supplier
        from sqlalchemy import or_, cast, String
        term = journal.search_term(query.strip())
        stmt = stmt.outerjoin(Supplier).filter(or_(*[col.ilike(term, escape="\\") for col in
            (PurchaseReceipt.number, PurchaseReceipt.note, Supplier.name, cast(PurchaseReceipt.lines, String))]))
    stmt = journal.period(stmt, PurchaseReceipt.occurred_at, date_from, date_to)
    return {
        "total": stmt.count(),
        "items": [
            service.receipt_out(row)
            for row in stmt.order_by(PurchaseReceipt.occurred_at.desc(), PurchaseReceipt.id.desc())
            .offset(offset)
            .limit(limit)
        ],
    }


@app.post("/receipts", status_code=201)
def receive(
    body: Receive,
    db: Session = Depends(get_db),
    user: User = Depends(require_edit(Module.WAREHOUSE)),
    accounting_user: User = Depends(view),
):
    try:
        return service.receive(db, body, user)
    except Exception:
        db.rollback()
        raise
