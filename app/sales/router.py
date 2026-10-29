from fastapi import FastAPI, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_
from sqlalchemy.orm import Session
from app.common.module_access import Module
from app.core.deps import require_view, require_edit, require_full
from app.db.session import get_db
from app.sales import service
from app.sales.models import SalesOrder, SalesShipment
from app.sales.schemas import (
    CreateOrder,
    EditOrder,
    Version,
    Archive,
    Cancel,
    CreateShipment,
    PostShipment,
)
from app.accounting.models import Counterparty, CounterpartyKind, Organization
from app.warehouse.models import WarehouseMaterial
from app.warehouse.reservation_service import allocations, dec
from app.users.models import User

app = FastAPI(title="Заказы покупателей и отгрузки")
view, edit, full = (
    require_view(Module.CLIENTS),
    require_edit(Module.CLIENTS),
    require_full(Module.CLIENTS),
)


@app.get("/catalog")
def catalog(db: Session = Depends(get_db), user: User = Depends(view)):
    materials = (
        db.query(WarehouseMaterial)
        .filter_by(is_archived=False)
        .order_by(WarehouseMaterial.title)
        .all()
    )
    holds = allocations(db, [m.id for m in materials])
    return {
        "counterparties": [
            {"id": c.id, "name": c.name, "inn": c.inn, "active": c.is_active}
            for c in db.query(Counterparty).order_by(Counterparty.name)
        ],
        "organizations": [
            {"id": o.id, "name": o.short_name}
            for o in db.query(Organization).filter_by(is_active=True)
        ],
        # Commercial catalogue, no purchasing costs. Needed by sales managers
        # even when they cannot inspect the warehouse register itself.
        "materials": [
            {
                "id": m.id,
                "title": m.title,
                "code": m.code,
                "unit": m.unit,
                "warehouse": m.warehouse.value,
                "is_fractional": m.is_fractional,
                "available_quantity": float(
                    max(0, dec(m.quantity_in_stock) - sum(holds.get(m.id, {}).values()))
                ),
            }
            for m in materials
        ],
    }


class Buyer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=255)
    inn: str | None = Field(None, max_length=16, pattern=r"^\d{10}(\d{2})?$")


@app.post("/buyers", status_code=201)
def create_buyer(
    body: Buyer, db: Session = Depends(get_db), user: User = Depends(edit)
):
    name = body.name.strip()
    if not name:
        service.fail("Укажите имя покупателя", 422)
    from app.warehouse.documents import advisory

    advisory(db, "sales-buyer:" + (body.inn or name.casefold()))
    if body.inn:
        old = db.query(Counterparty).filter_by(inn=body.inn).first()
    else:
        old = next(
            (c for c in db.query(Counterparty) if c.name.casefold() == name.casefold()),
            None,
        )
    if old:
        if not old.is_active:
            service.fail(
                "Такой контрагент находится в архиве. Восстановите его в справочнике."
            )
        return {"id": old.id, "name": old.name, "inn": old.inn, "active": old.is_active}
    row = Counterparty(
        name=name, inn=body.inn, kind=CounterpartyKind.OTHER, is_active=True
    )
    db.add(row)
    db.commit()
    return {"id": row.id, "name": row.name, "inn": row.inn, "active": row.is_active}


def search(q, cls, query, archive):
    if archive != "all":
        q = q.filter(cls.archived.is_(archive == "archived"))
    if query.strip():
        term = "%" + query.strip().replace("%", "\\%").replace("_", "\\_") + "%"
        q = q.join(Counterparty).filter(
            or_(
                cls.number.ilike(term, escape="\\"),
                Counterparty.name.ilike(term, escape="\\"),
            )
        )
    return q


@app.get("/orders")
def orders(
    query: str = "",
    archive: str = Query("active", pattern="^(active|archived|all)$"),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(view),
):
    q = search(db.query(SalesOrder), SalesOrder, query, archive)
    return {
        "total": q.count(),
        "items": [
            service.order_out(db, r)
            for r in q.order_by(SalesOrder.occurred_at.desc(), SalesOrder.id.desc())
            .offset(offset)
            .limit(limit)
        ],
    }


@app.get("/orders/{id}")
def order(id: int, db: Session = Depends(get_db), user: User = Depends(view)):
    return service.order_out(db, service.get_order(db, id), True)


@app.post("/orders", status_code=201)
def create_order(
    body: CreateOrder, db: Session = Depends(get_db), user: User = Depends(edit)
):
    return service.create_order(db, body, user)


@app.patch("/orders/{id}")
def edit_order(
    id: int, body: EditOrder, db: Session = Depends(get_db), user: User = Depends(edit)
):
    return service.edit_order(db, id, body, user)


@app.post("/orders/{id}/confirm")
def confirm_order(
    id: int, body: Version, db: Session = Depends(get_db), user: User = Depends(edit)
):
    return service.confirm_order(db, id, body, user)


@app.post("/orders/{id}/cancel")
def cancel_order(
    id: int, body: Cancel, db: Session = Depends(get_db), user: User = Depends(edit)
):
    return service.cancel_order(db, id, body, user)


@app.post("/orders/{id}/archive")
def archive_order(
    id: int, body: Archive, db: Session = Depends(get_db), user: User = Depends(full)
):
    return service.archive_order(db, id, body, user)


@app.get("/shipments")
def shipments(
    query: str = "",
    order_id: int | None = None,
    archive: str = Query("active", pattern="^(active|archived|all)$"),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(view),
):
    q = search(db.query(SalesShipment), SalesShipment, query, archive)
    if order_id is not None:
        q = q.filter_by(order_id=order_id)
    return {
        "total": q.count(),
        "items": [
            service.shipment_out(db, r)
            for r in q.order_by(
                SalesShipment.occurred_at.desc(), SalesShipment.id.desc()
            )
            .offset(offset)
            .limit(limit)
        ],
    }


@app.get("/shipments/{id}")
def shipment(id: int, db: Session = Depends(get_db), user: User = Depends(view)):
    return service.shipment_out(db, service.get_shipment(db, id), True)


@app.post("/shipments", status_code=201)
def create_shipment(
    body: CreateShipment, db: Session = Depends(get_db), user: User = Depends(edit)
):
    return service.create_shipment(db, body, user)


@app.post("/shipments/{id}/post")
def post_shipment(
    id: int,
    body: PostShipment,
    db: Session = Depends(get_db),
    user: User = Depends(require_edit(Module.WAREHOUSE)),
    sales_user: User = Depends(view),
):
    return service.post_shipment(db, id, body, user)


@app.post("/shipments/{id}/cancel")
def cancel_shipment(
    id: int,
    body: Cancel,
    db: Session = Depends(get_db),
    user: User = Depends(edit),
):
    return service.cancel_shipment(db, id, body, user)


@app.post("/shipments/{id}/archive")
def archive_shipment(
    id: int, body: Archive, db: Session = Depends(get_db), user: User = Depends(full)
):
    return service.archive_shipment(db, id, body, user)
