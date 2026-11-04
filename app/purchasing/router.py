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
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(view),
):
    stmt = db.query(SupplierOrder)
    if archive != "all":
        stmt = stmt.filter(SupplierOrder.archived.is_(archive == "archived"))
    if query:
        from app.warehouse.models import Supplier
        from sqlalchemy import or_, cast, String

        term = "%" + query.replace("%", "\\%").replace("_", "\\_") + "%"
        stmt = stmt.join(Supplier).filter(
            or_(
                SupplierOrder.number.ilike(term, escape="\\"),
                Supplier.name.ilike(term, escape="\\"),
                cast(SupplierOrder.items, String).ilike(term, escape="\\"),
            )
        )
    return {
        "total": stmt.count(),
        "items": [
            service.out(db, row)
            for row in stmt.order_by(SupplierOrder.created_at.desc(), SupplierOrder.id.desc())
            .offset(offset)
            .limit(limit)
        ],
    }


@app.get("/orders/{id}")
def order(id: int, db: Session = Depends(get_db), user: User = Depends(view)):
    return service.out(db, service.get_order(db, id))


@app.get("/orders/{id}/automatic-review")
def automatic_review(id: int, db: Session = Depends(get_db), user: User = Depends(view)):
    """Read-only readiness check. Unverified fields must never become approval."""
    row = service.get_order(db, id)
    reasons = [
        {"code": "supplier_verification_missing", "message": "Статус проверенного поставщика не подтверждён в системе."},
        {"code": "sale_link_missing", "message": "Связь заказа с согласованной продажей не подтверждена."},
        {"code": "techcard_link_missing", "message": "Связь с действующей техкартой и нормативом не подтверждена."},
        {"code": "price_evidence_missing", "message": "Предыдущая и текущая сопоставимые цены не подтверждены документами."},
        {"code": "payment_controls_missing", "message": "Лимиты и банковские проверки для автоматической оплаты не настроены."},
    ]
    if not row.items:
        reasons.append({"code": "no_items", "message": "В заказе нет позиций."})
    if row.archived:
        reasons.append({"code": "archived", "message": "Заказ находится в архиве."})
    if any(not item.get("warehouse_material_id") or not item.get("unit") for item in row.items):
        reasons.append({"code": "item_mapping_missing", "message": "Для некоторых позиций не указан товар склада или единица измерения."})
    return {"order_id": row.id, "decision": "manual_review", "auto_approval_enabled": False, "auto_payment_enabled": False,
            "reasons": reasons}


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
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(view),
):
    stmt = db.query(PurchaseReceipt)
    if order_id is not None:
        stmt = stmt.filter_by(order_id=order_id)
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
