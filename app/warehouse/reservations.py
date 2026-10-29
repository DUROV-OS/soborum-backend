from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func
from sqlalchemy.orm import Session
from app.common.module_access import Module
from app.core.deps import require_edit, require_full, require_view
from app.db.session import get_db
from app.users.models import User
from app.warehouse import service
from app.warehouse.models import (
    WarehouseMaterial,
    WarehouseOperation,
    WarehouseOperationKind,
    IssueDestinationKind,
    StockMovementReason,
)
from app.warehouse.reservation_models import StockReservation, ReservationEvent
from app.warehouse.reservation_service import (
    REMAINING,
    remaining,
    dec,
    available,
    invalidate_cache as invalidate,
)
from app.warehouse.documents import advisory
from app.moysklad.models import SourceRecord, SourceLink
from app.moysklad.importer import ref, rows
from app.production.models import Production
from app.cycle.models import CycleStatus

router = APIRouter(prefix="/reservations")
view, edit, full = (
    require_view(Module.WAREHOUSE),
    require_edit(Module.WAREHOUSE),
    require_full(Module.WAREHOUSE),
)


class Create(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    material_id: int = Field(gt=0)
    quantity: Decimal = Field(
        gt=0, max_digits=18, decimal_places=6, allow_inf_nan=False
    )
    purpose: str = Field(default="", max_length=255)
    production_id: int | None = Field(default=None, gt=0)
    source_order_id: int | None = Field(default=None, gt=0)
    source_line: int | None = Field(default=None, ge=0)


class Change(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    version: int = Field(ge=1)
    quantity: Decimal = Field(
        gt=0, max_digits=18, decimal_places=6, allow_inf_nan=False
    )
    note: str = Field(min_length=1, max_length=500)


class Issue(Change):
    received_by: str = Field(min_length=1, max_length=255)
    occurred_at: datetime | None = None


class Archive(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)
    archived: bool


def fail(message, code=409):
    raise HTTPException(code, message)


def get(db, id, lock=False):
    query = db.query(StockReservation).filter_by(id=id)
    if lock:
        query = query.populate_existing().with_for_update(of=StockReservation)
    row = query.first()
    if not row:
        fail("Резерв не найден", 404)
    return row


def out(db, row):
    m = db.get(WarehouseMaterial, row.material_id)
    left = remaining(row)
    free = available(db, m)
    held = dec(m.quantity_in_stock) - free
    deficit = max(Decimal(0), held - max(Decimal(0), dec(m.quantity_in_stock)))
    state = (
        "active"
        if left > 0
        else (
            "issued"
            if dec(row.consumed_quantity) == dec(row.quantity)
            else (
                "released"
                if dec(row.released_quantity) == dec(row.quantity)
                else "closed"
            )
        )
    )
    return {
        "id": row.id,
        "material_id": m.id,
        "material_title": m.title,
        "material_code": m.code,
        "warehouse": m.warehouse.value,
        "unit": row.unit,
        "production_id": row.production_id,
        "source_order_id": row.source_order_id,
        "source_line": row.source_line,
        "purpose": row.purpose,
        "quantity": float(row.quantity),
        "remaining_quantity": float(left),
        "released_quantity": float(row.released_quantity),
        "consumed_quantity": float(row.consumed_quantity),
        "available_quantity": float(max(Decimal(0), free)),
        "available_for_issue": float(max(Decimal(0), free + left)),
        "shortage": float(deficit) if left > 0 else 0,
        "version": row.version,
        "archived": row.archived,
        "status": state,
        "created_at": row.created_at,
    }


def order_capacity(db, source, index, exclude_id=None):
    line = rows(source.payload.get("positions"))[index]
    committed = db.query(
        func.sum(StockReservation.quantity - StockReservation.released_quantity)
    ).filter(
        StockReservation.source_order_id == source.id,
        StockReservation.source_line == index,
    )
    if exclude_id:
        committed = committed.filter(StockReservation.id != exclude_id)
    return max(
        Decimal(0),
        dec(line.get("quantity")) - dec(line.get("shipped")) - dec(committed.scalar()),
    )


@router.get("/catalog")
def catalog(db: Session = Depends(get_db), user: User = Depends(view)):
    orders = []
    active_ids = {
        mid for (mid,) in db.query(WarehouseMaterial.id).filter_by(is_archived=False)
    }
    links = {
        l.source_key: l.target_id
        for l in db.query(SourceLink).filter_by(target_type="warehouse_materials")
    }
    for source in db.query(SourceRecord).filter_by(
        entity_type="customerorder", archived=False
    ):
        if not source.payload.get("applicable"):
            continue
        positions = []
        for index, line in enumerate(rows(source.payload.get("positions"))):
            mid = links.get(
                "stock/"
                + ref(line.get("assortment"))
                + "/"
                + ref(source.payload.get("store"))
            )
            capacity = order_capacity(db, source, index)
            if mid in active_ids and capacity > 0:
                positions.append(
                    {"index": index, "material_id": mid, "capacity": float(capacity)}
                )
        if positions:
            orders.append(
                {
                    "id": source.id,
                    "name": "Заказ покупателя № " + source.title,
                    "lines": positions,
                }
            )
    from app.cycle.models import Cycle

    houses = [
        {"id": p.id, "name": service.house_label(p)}
        for p in db.query(Production)
        .join(Cycle, Production.cycle_id == Cycle.id)
        .filter(Cycle.status != CycleStatus.COMPLETED)
        .order_by(Production.id)
    ]
    return {"orders": orders, "houses": houses}


@router.get("")
def listing(
    material_id: int | None = None,
    production_id: int | None = None,
    status: str = Query("active", pattern="^(active|closed|all)$"),
    archive: str = Query("active", pattern="^(active|archived|all)$"),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(view),
):
    query = db.query(StockReservation)
    if material_id is not None:
        query = query.filter_by(material_id=material_id)
    if production_id is not None:
        query = query.filter_by(production_id=production_id)
    if archive != "all":
        query = query.filter_by(archived=archive == "archived")
    if status != "all":
        query = query.filter(REMAINING > 0 if status == "active" else REMAINING <= 0)
    return {
        "total": query.count(),
        "items": [
            out(db, r)
            for r in query.order_by(StockReservation.id.desc())
            .offset(offset)
            .limit(limit)
        ],
    }


@router.get("/{id}")
def detail(id: int, db: Session = Depends(get_db), user: User = Depends(view)):
    row = get(db, id)
    events = [
        {
            "id": e.id,
            "kind": e.kind,
            "quantity": float(e.quantity),
            "actor_id": e.actor_id,
            "movement_id": e.movement_id,
            "note": e.note,
            "created_at": e.created_at,
        }
        for e in db.query(ReservationEvent)
        .filter_by(reservation_id=id)
        .order_by(ReservationEvent.id)
    ]
    return {**out(db, row), "events": events}


@router.post("", status_code=201)
def create(body: Create, db: Session = Depends(get_db), user: User = Depends(edit)):
    parameters = body.model_dump(mode="json")
    advisory(db, "reserve:" + str(body.request_id))
    old = db.query(StockReservation).filter_by(request_id=str(body.request_id)).first()
    if old:
        if old.actor_id != user.id or old.parameters != parameters:
            fail("Номер запроса уже использован с другими данными")
        return out(db, old)
    if body.production_id and body.source_order_id:
        fail("Выберите один заказ или дом", 422)
    if (body.source_order_id is None) != (body.source_line is None):
        fail("Укажите заказ покупателя и его строку", 422)
    purpose = body.purpose.strip()
    norms = None
    if body.production_id:
        production = db.get(Production, body.production_id)
        if not production or production.cycle.status == CycleStatus.COMPLETED:
            fail("Выберите действующий дом", 422)
        from app.production.locks import lock_house_materials

        norms = lock_house_materials(db, production.id)
        purpose = purpose or service.house_label(production)
    if body.source_order_id:
        source = (
            db.query(SourceRecord)
            .filter_by(
                id=body.source_order_id, entity_type="customerorder", archived=False
            )
            .populate_existing()
            .with_for_update(of=SourceRecord)
            .first()
        )
        if not source or not source.payload.get("applicable"):
            fail("Заказ покупателя недоступен", 422)
        positions = rows(source.payload.get("positions"))
        if body.source_line >= len(positions):
            fail("Строка заказа не найдена", 422)
        key = (
            "stock/"
            + ref(positions[body.source_line].get("assortment"))
            + "/"
            + ref(source.payload.get("store"))
        )
        link = (
            db.query(SourceLink)
            .filter_by(
                source_key=key,
                target_type="warehouse_materials",
                target_id=body.material_id,
            )
            .first()
        )
        if not link:
            fail("Материал или склад не совпадает со строкой заказа", 422)
        if body.quantity > order_capacity(db, source, body.source_line):
            fail(
                "Резерв превышает остаток заказа с учётом уже выданного и зарезервированного"
            )
        purpose = purpose or "Заказ покупателя № " + source.title
    if not purpose:
        fail("Укажите назначение резерва", 422)
    m = service._lock_materials(db, [body.material_id]).get(body.material_id)
    if not m or m.is_archived:
        fail("Выберите действующий материал", 422)
    if not m.is_fractional and body.quantity % 1:
        fail("Материал учитывается целыми единицами", 422)
    if body.quantity > available(db, m):
        fail("Недостаточно свободного остатка для резерва")
    if norms is not None:
        need = sum(
            (
                dec(n.quantity_required) + dec(n.quantity_requested)
                for n in norms
                if n.warehouse_material_id == m.id
            ),
            Decimal(0),
        )
        held = dec(
            db.query(func.sum(REMAINING))
            .filter(
                StockReservation.production_id == body.production_id,
                StockReservation.material_id == m.id,
                REMAINING > 0,
            )
            .scalar()
        )
        if body.quantity > need - held:
            fail("Резерв превышает невыданный норматив дома")
    row = StockReservation(
        material_id=m.id,
        production_id=body.production_id,
        source_order_id=body.source_order_id,
        source_line=body.source_line,
        purpose=purpose[:255],
        unit=m.unit,
        quantity=body.quantity,
        released_quantity=0,
        consumed_quantity=0,
        actor_id=user.id,
        request_id=str(body.request_id),
        parameters=parameters,
        version=1,
        archived=False,
    )
    db.add(row)
    db.flush()
    db.add(
        ReservationEvent(
            reservation_id=row.id,
            kind="created",
            quantity=body.quantity,
            actor_id=user.id,
            note=purpose[:500],
            parameters={},
        )
    )
    service.sync_shortage_task(db, m)
    invalidate(db, row.production_id)
    db.commit()
    return out(db, row)


def locked_change(db, id, body, user, kind):
    advisory(db, "reserve-event:" + str(body.request_id))
    event = (
        db.query(ReservationEvent).filter_by(request_id=str(body.request_id)).first()
    )
    if event:
        if (
            event.reservation_id != id
            or event.actor_id != user.id
            or event.kind != kind
            or event.parameters != body.model_dump(mode="json")
        ):
            fail("Номер запроса уже использован с другими данными")
        return get(db, id), None, True
    initial = get(db, id)
    m = service._lock_materials(db, [initial.material_id])[initial.material_id]
    row = get(db, id, True)
    if row.archived or row.version != body.version:
        fail("Резерв изменён. Откройте карточку заново.")
    if body.quantity > remaining(row):
        fail("Количество превышает остаток резерва")
    if not m.is_fractional and body.quantity % 1:
        fail("Материал учитывается целыми единицами", 422)
    if not body.note.strip():
        fail("Укажите основание действия", 422)
    return row, m, False


@router.post("/{id}/release")
def release(
    id: int, body: Change, db: Session = Depends(get_db), user: User = Depends(edit)
):
    row, m, retry = locked_change(db, id, body, user, "released")
    if retry:
        return out(db, row)
    row.released_quantity = dec(row.released_quantity) + body.quantity
    row.version += 1
    db.add(
        ReservationEvent(
            reservation_id=id,
            kind="released",
            quantity=body.quantity,
            actor_id=user.id,
            request_id=str(body.request_id),
            parameters=body.model_dump(mode="json"),
            note=body.note.strip(),
        )
    )
    service.sync_shortage_task(db, m)
    invalidate(db, row.production_id)
    db.commit()
    return out(db, row)


@router.post("/{id}/issue")
def issue(
    id: int, body: Issue, db: Session = Depends(get_db), user: User = Depends(edit)
):
    row, m, retry = locked_change(db, id, body, user, "issued")
    if retry:
        return out(db, row)
    if row.production_id:
        fail("Для дома используйте выдачу по техкарте или заявке производства")
    if row.source_order_id:
        source = db.get(SourceRecord, row.source_order_id)
        if source.archived or not source.payload.get("applicable"):
            fail("Заказ закрыт или не проведён; снимите резерв")
    if (
        m.is_archived
        or m.unit != row.unit
        or (not m.is_fractional and body.quantity % 1)
    ):
        fail("Карточка материала изменилась", 422)
    receiver = body.received_by.strip()
    if not receiver:
        fail("Укажите получателя", 422)
    if body.quantity > available(db, m) + remaining(row):
        fail("На складе не хватает товара с учётом других резервов")
    occurred = service.resolve_occurred_at(body.occurred_at)
    op = WarehouseOperation(
        kind=WarehouseOperationKind.ISSUE_MANUAL,
        occurred_at=occurred,
        destination_kind=IssueDestinationKind.OBJECT,
        destination=row.purpose,
        received_by=receiver,
        note=("Выдача из резерва № " + str(id) + ". " + body.note)[:500],
        created_by_id=user.id,
    )
    db.add(op)
    db.flush()
    m.quantity_in_stock = dec(m.quantity_in_stock) - body.quantity
    movement = service.log_movement(
        db,
        m,
        -body.quantity,
        StockMovementReason.ISSUED_MANUAL,
        user,
        note=op.note,
        operation_id=op.id,
    )
    row.consumed_quantity = dec(row.consumed_quantity) + body.quantity
    row.version += 1
    db.add(
        ReservationEvent(
            reservation_id=id,
            kind="issued",
            quantity=body.quantity,
            actor_id=user.id,
            request_id=str(body.request_id),
            parameters=body.model_dump(mode="json"),
            note=body.note.strip(),
            movement_id=movement.id,
        )
    )
    service.sync_shortage_task(db, m)
    invalidate(db)
    db.commit()
    return out(db, row)


@router.post("/{id}/archive")
def archive(
    id: int, body: Archive, db: Session = Depends(get_db), user: User = Depends(full)
):
    initial = get(db, id)
    service._lock_materials(db, [initial.material_id])
    row = get(db, id, True)
    if row.version != body.version or remaining(row) > 0:
        fail("Архивировать можно только закрытый резерв. Обновите карточку.")
    row.archived = body.archived
    row.version += 1
    db.commit()
    return out(db, row)
