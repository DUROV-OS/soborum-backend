from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from fastapi import HTTPException
from sqlalchemy import or_
from app.common.module_access import Module, AccessLevel
from app.accounting.models import Counterparty, Organization
from app.sales.models import SalesOrder, SalesShipment, SalesEvent
from app.users.models import User
from app.warehouse import service as stock
from app.warehouse.documents import advisory
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
    invalidate_cache,
)


def fail(message, code=409):
    raise HTTPException(code, message)


def money(value):
    return dec(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def get_order(db, id, lock=False):
    q = db.query(SalesOrder).filter_by(id=id)
    if lock:
        q = q.populate_existing().with_for_update(of=SalesOrder)
    row = q.first()
    if not row:
        fail("Заказ покупателя не найден", 404)
    return row


def get_shipment(db, id, lock=False):
    q = db.query(SalesShipment).filter_by(id=id)
    if lock:
        q = q.populate_existing().with_for_update(of=SalesShipment)
    row = q.first()
    if not row:
        fail("Отгрузка не найдена", 404)
    return row


def parties(db, counterparty_id, organization_id):
    cp, org = db.get(Counterparty, counterparty_id), db.get(
        Organization, organization_id
    )
    if not cp or not cp.is_active or not org or not org.is_active:
        fail("Выберите действующего покупателя и организацию", 422)
    return cp, org


def event(db, order, user, kind, data, shipment=None, request_id=None):
    db.add(
        SalesEvent(
            order_id=order.id if order else None,
            shipment_id=shipment.id if shipment else None,
            actor_id=user.id,
            kind=kind,
            data=data,
            request_id=str(request_id) if request_id else None,
        )
    )


def history(db, *, order_id=None, shipment_id=None):
    q = db.query(SalesEvent)
    q = (
        q.filter_by(shipment_id=shipment_id)
        if shipment_id
        else q.filter_by(order_id=order_id)
    )
    return [
        {
            "id": e.id,
            "kind": e.kind,
            "actor_id": e.actor_id,
            "actor_name": db.get(User, e.actor_id).full_name,
            "created_at": e.created_at,
            "data": e.data,
            "shipment_id": e.shipment_id,
        }
        for e in q.order_by(SalesEvent.id)
    ]


def shipped(db, order):
    result = {i: dec(l.get("opening_shipped")) for i, l in enumerate(order.lines)}
    for row in db.query(SalesShipment).filter_by(
        order_id=order.id, status="posted", historical=False
    ):
        for line in row.lines:
            index = line["order_line"]
            result[index] = result.get(index, Decimal(0)) + dec(line["quantity"])
    return result


def order_holds(db, order, *, material_id=None, lock=False):
    # Source-bound holds predating native orders are recognized until the
    # one-time importer converts their target without replaying a movement.
    target = StockReservation.sales_order_id == order.id
    if order.source_record_id:
        target = or_(target, StockReservation.source_order_id == order.source_record_id)
    q = db.query(StockReservation).filter(target, REMAINING > 0)
    if material_id is not None:
        q = q.filter_by(material_id=material_id)
    q = q.order_by(StockReservation.id)
    return (
        q.populate_existing().with_for_update(of=StockReservation) if lock else q
    ).all()


def remaining_to_reserve(db, order, index):
    if index >= len(order.lines):
        fail("Строка заказа не найдена", 422)
    held = sum(
        (
            remaining(r)
            for r in order_holds(db, order)
            if (r.sales_line if r.sales_order_id else r.source_line) == index
        ),
        Decimal(0),
    )
    return max(
        Decimal(0),
        dec(order.lines[index]["quantity"]) - shipped(db, order).get(index, 0) - held,
    )


def order_out(db, row, detail=False):
    cp, org = db.get(Counterparty, row.counterparty_id), db.get(
        Organization, row.organization_id
    )
    done, held = shipped(db, row), defaultdict(Decimal)
    for r in order_holds(db, row):
        held[r.sales_line if r.sales_order_id else r.source_line] += remaining(r)
    lines = [
        {
            **l,
            "order_line": i,
            "shipped_quantity": float(done.get(i, 0)),
            "remaining_quantity": float(
                max(Decimal(0), dec(l["quantity"]) - done.get(i, 0))
            ),
            "reserved_quantity": float(held[i]),
        }
        for i, l in enumerate(row.lines)
    ]
    result = {
        k: getattr(row, k)
        for k in (
            "id",
            "source_id",
            "source_record_id",
            "number",
            "counterparty_id",
            "organization_id",
            "status",
            "source_state",
            "archived",
            "version",
            "currency",
            "expected_at",
            "address",
            "note",
            "occurred_at",
            "created_at",
        )
    }
    result.update(
        counterparty_name=cp.name,
        organization_name=org.short_name,
        total=float(row.total),
        opening_paid=float(row.opening_paid),
        lines=lines,
        fulfillment=(
            "complete"
            if all(l["remaining_quantity"] == 0 for l in lines)
            else "partial" if any(done.values()) else "waiting"
        ),
        financial_fields_locked=frozen(db, row),
    )
    if detail:
        result["events"] = history(db, order_id=row.id)
        result["shipments"] = [
            shipment_out(db, s)
            for s in db.query(SalesShipment)
            .filter_by(order_id=row.id)
            .order_by(SalesShipment.id.desc())
        ]
    return result


def shipment_out(db, row, detail=False):
    result = {
        k: getattr(row, k)
        for k in (
            "id",
            "source_id",
            "order_id",
            "counterparty_id",
            "organization_id",
            "number",
            "status",
            "historical",
            "archived",
            "version",
            "currency",
            "lines",
            "received_by",
            "address",
            "note",
            "actor_id",
            "occurred_at",
            "operation_id",
            "posted_at",
            "posted_by_id",
            "cancel_operation_id",
            "cancelled_by_id",
            "cancelled_at",
            "cancellation_note",
        )
    }
    result.update(
        total=float(row.total),
        counterparty_name=db.get(Counterparty, row.counterparty_id).name,
        organization_name=db.get(Organization, row.organization_id).short_name,
    )
    result["lines"] = [
        {k: v for k, v in l.items() if k != "stock_unit_cost"} for l in row.lines
    ]
    if detail:
        result["events"] = history(db, shipment_id=row.id)
        result["order_version"] = (
            get_order(db, row.order_id).version if row.order_id else None
        )
    return result


def normalize_lines(db, entries):
    materials = {
        m.id: m
        for m in db.query(WarehouseMaterial).filter(
            WarehouseMaterial.id.in_([l.material_id for l in entries if l.material_id])
        )
    }
    lines, total = [], Decimal(0)
    for l in entries:
        m = materials.get(l.material_id)
        if l.stock_tracked:
            if not m or m.is_archived:
                fail("Выберите действующую складскую карточку для товара", 422)
            if not m.is_fractional and l.quantity % 1:
                fail("Материал «" + m.title + "» учитывается целыми единицами", 422)
        elif l.material_id or not l.title.strip() or not l.unit.strip():
            fail("Для услуги укажите название и единицу без складской карточки", 422)
        amount = money(l.quantity * l.unit_price)
        total += amount
        lines.append(
            {
                "material_id": m.id if m else None,
                "title": m.title if m else l.title.strip(),
                "unit": m.unit if m else l.unit.strip(),
                "stock_tracked": l.stock_tracked,
                "quantity": str(l.quantity),
                "unit_price": str(l.unit_price),
                "line_total": str(amount),
                "opening_shipped": "0",
            }
        )
    if total >= Decimal("1000000000000"):
        fail("Сумма заказа превышает допустимую", 422)
    return lines, total


def create_order(db, body, user):
    advisory(db, "sales-order:" + str(body.request_id))
    params = body.model_dump(mode="json")
    old = db.query(SalesOrder).filter_by(request_id=str(body.request_id)).first()
    if old:
        if old.actor_id != user.id or old.parameters != params:
            fail("Номер запроса уже использован для другого заказа")
        return order_out(db, old, True)
    parties(db, body.counterparty_id, body.organization_id)
    lines, total = normalize_lines(db, body.lines)
    row = SalesOrder(
        request_id=str(body.request_id),
        parameters=params,
        counterparty_id=body.counterparty_id,
        organization_id=body.organization_id,
        lines=lines,
        total=total,
        expected_at=body.expected_at,
        address=body.address.strip(),
        note=body.note.strip(),
        actor_id=user.id,
        occurred_at=datetime.now(timezone.utc),
    )
    db.add(row)
    db.flush()
    row.number = "ЗП-" + str(row.id)
    event(db, row, user, "order_created", params)
    db.commit()
    return order_out(db, row, True)


def frozen(db, order):
    return bool(
        order.source_id
        or db.query(SalesShipment.id).filter_by(order_id=order.id).first()
        or db.query(StockReservation.id).filter_by(sales_order_id=order.id).first()
    )


def edit_order(db, id, body, user):
    row = get_order(db, id, True)
    if row.version != body.version or row.archived or row.status == "cancelled":
        fail("Заказ изменён, отменён или находится в архиве. Обновите карточку.")
    fields = body.model_dump(exclude_unset=True, exclude={"version"})
    if frozen(db, row) and any(
        k in fields for k in ("counterparty_id", "organization_id", "lines")
    ):
        fail(
            "После переноса, резерва или отгрузки состав и стороны заказа не меняются. Создайте отдельный заказ."
        )
    parties(
        db,
        fields.get("counterparty_id", row.counterparty_id),
        fields.get("organization_id", row.organization_id),
    )
    if "lines" in fields:
        if body.lines is None:
            fail("Добавьте строки заказа", 422)
        fields["lines"], row.total = normalize_lines(db, body.lines)
    for k, v in fields.items():
        if k not in {"expected_at"} and v is None:
            fail("Поле не может быть пустым: " + k, 422)
        setattr(row, k, v.strip() if isinstance(v, str) else v)
    row.version += 1
    event(
        db, row, user, "order_edited", body.model_dump(mode="json", exclude_unset=True)
    )
    db.commit()
    return order_out(db, row, True)


def confirm_order(db, id, body, user):
    row = get_order(db, id, True)
    if row.version != body.version or row.archived or row.status != "draft":
        fail("Подтвердить можно только действующий черновик. Обновите заказ.")
    parties(db, row.counterparty_id, row.organization_id)
    row.status = "confirmed"
    row.version += 1
    event(db, row, user, "order_confirmed", {})
    db.commit()
    return order_out(db, row, True)


def archive_order(db, id, body, user):
    row = get_order(db, id, True)
    if row.version != body.version:
        fail("Заказ изменён. Обновите карточку.")
    if body.archived and (
        order_holds(db, row)
        or db.query(SalesShipment.id).filter_by(order_id=row.id, status="draft").first()
    ):
        fail("Сначала снимите резервы и завершите черновики отгрузок")
    row.archived = body.archived
    row.version += 1
    event(db, row, user, "order_archived" if body.archived else "order_restored", {})
    db.commit()
    return order_out(db, row, True)


def cancel_order(db, id, body, user):
    advisory(db, "sales-action:" + str(body.request_id))
    old = db.query(SalesEvent).filter_by(request_id=str(body.request_id)).first()
    params = body.model_dump(mode="json")
    if old:
        if (
            old.order_id != id
            or old.kind != "order_cancelled"
            or old.actor_id != user.id
            or old.data != params
        ):
            fail("Номер запроса уже использован с другими данными")
        return order_out(db, get_order(db, id), True)
    row = get_order(db, id, True)
    if row.version != body.version or row.archived or row.status == "cancelled":
        fail("Заказ изменён, отменён или архивирован")
    if not body.note.strip():
        fail("Укажите причину отмены", 422)
    if (
        order_holds(db, row)
        or any(shipped(db, row).values())
        or db.query(SalesShipment.id).filter_by(order_id=id, status="draft").first()
    ):
        fail(
            "Сначала завершите отгрузки и снимите резервы. Исполненный заказ нельзя отменить."
        )
    row.status = "cancelled"
    row.version += 1
    event(db, row, user, "order_cancelled", params, request_id=body.request_id)
    db.commit()
    return order_out(db, row, True)


def create_shipment(db, body, user):
    advisory(db, "sales-shipment:" + str(body.request_id))
    params = body.model_dump(mode="json")
    old = db.query(SalesShipment).filter_by(request_id=str(body.request_id)).first()
    if old:
        if old.actor_id != user.id or old.parameters != params:
            fail("Номер запроса уже использован для другой отгрузки")
        return shipment_out(db, old, True)
    order = get_order(db, body.order_id, True)
    if (
        order.version != body.order_version
        or order.archived
        or order.status != "confirmed"
    ):
        fail("Выберите подтверждённый действующий заказ и обновите его версию")
    parties(db, order.counterparty_id, order.organization_id)
    if not body.received_by.strip() or body.occurred_at.tzinfo is None:
        fail("Укажите получателя и дату с часовым поясом", 422)
    occurred = stock.resolve_occurred_at(body.occurred_at)
    done, seen, lines = shipped(db, order), set(), []
    for l in body.lines:
        if l.order_line in seen or l.order_line >= len(order.lines):
            fail("Каждую строку заказа укажите один раз", 422)
        seen.add(l.order_line)
        item = order.lines[l.order_line]
        if l.quantity > dec(item["quantity"]) - done.get(l.order_line, 0):
            fail("Отгрузка превышает невыданный остаток заказа")
        lines.append(
            {
                **item,
                "order_line": l.order_line,
                "quantity": str(l.quantity),
                "line_total": str(money(l.quantity * dec(item["unit_price"]))),
            }
        )
    validate_materials(db, lines, lock=False)
    row = SalesShipment(
        request_id=str(body.request_id),
        parameters=params,
        order_id=order.id,
        counterparty_id=order.counterparty_id,
        organization_id=order.organization_id,
        lines=lines,
        total=sum((dec(l["line_total"]) for l in lines), Decimal(0)),
        occurred_at=occurred,
        received_by=body.received_by.strip(),
        address=body.address.strip(),
        note=body.note.strip(),
        actor_id=user.id,
    )
    db.add(row)
    db.flush()
    row.number = "ОТ-" + str(row.id)
    event(db, order, user, "shipment_created", params, row)
    db.commit()
    return shipment_out(db, row, True)


def validate_materials(db, lines, lock=True, allow_archived=False):
    ids = sorted(
        {
            l["material_id"]
            for l in lines
            if l.get("stock_tracked", True) and l.get("material_id")
        }
    )
    materials = (
        stock._lock_materials(db, ids)
        if lock
        else {
            m.id: m
            for m in db.query(WarehouseMaterial).filter(WarehouseMaterial.id.in_(ids))
        }
    )
    for l in lines:
        if not l.get("stock_tracked", True):
            continue
        m = materials.get(l.get("material_id"))
        if not m or (m.is_archived and not allow_archived) or m.unit != l["unit"]:
            fail("Складская карточка недоступна или её единица изменилась", 422)
        if not m.is_fractional and dec(l["quantity"]) % 1:
            fail("Материал «" + m.title + "» учитывается целыми единицами", 422)
    if len({m.warehouse for m in materials.values()}) > 1:
        fail(
            "Одна отгрузка относится к одному складу. Разделите выдачу по складам.", 422
        )
    return materials


def post_shipment(db, id, body, user):
    advisory(db, "sales-post:" + str(body.request_id))
    initial = get_shipment(db, id)
    order = get_order(db, initial.order_id, True) if initial.order_id else None
    row = get_shipment(db, id, True)
    params = body.model_dump(mode="json")
    if row.post_request_id == str(body.request_id):
        if row.posted_by_id != user.id or row.post_parameters != params:
            fail("Номер запроса уже использован с другими данными")
        return shipment_out(db, row, True)
    if (
        db.query(SalesShipment.id)
        .filter_by(post_request_id=str(body.request_id))
        .first()
    ):
        fail("Номер запроса уже использован для другой отгрузки")
    if (
        row.historical
        or row.archived
        or row.status != "draft"
        or row.version != body.version
    ):
        fail("Провести можно только новый действующий черновик. Обновите отгрузку.")
    if (
        not order
        or order.archived
        or order.status != "confirmed"
        or order.version != body.order_version
    ):
        fail("Заказ изменён или недоступен. Обновите карточку.")
    parties(db, order.counterparty_id, order.organization_id)
    done = shipped(db, order)
    for l in row.lines:
        if dec(l["quantity"]) > dec(
            order.lines[l["order_line"]]["quantity"]
        ) - done.get(l["order_line"], 0):
            fail("Остаток заказа уже отгружен другим документом")
    materials = validate_materials(db, row.lines)
    quantities = defaultdict(Decimal)
    for l in row.lines:
        if l.get("stock_tracked", True):
            quantities[l["material_id"]] += dec(l["quantity"])
    for mid, q in quantities.items():
        requested = {
            l["order_line"]: dec(l["quantity"])
            for l in row.lines
            if l.get("material_id") == mid
        }
        held = defaultdict(Decimal)
        for r in order_holds(db, order, material_id=mid):
            held[r.sales_line if r.sales_order_id else r.source_line] += remaining(r)
        own = sum(
            (min(held[index], quantity) for index, quantity in requested.items()),
            Decimal(0),
        )
        if q > available(db, materials[mid]) + own:
            fail("Недостаточно товара с учётом чужих резервов: " + materials[mid].title)
    op = None
    if quantities:
        op = WarehouseOperation(
            kind=WarehouseOperationKind.ISSUE_MANUAL,
            occurred_at=row.occurred_at,
            destination_kind=IssueDestinationKind.OBJECT,
            destination=("Заказ " + order.number)[:255],
            received_by=row.received_by,
            note=("Отгрузка " + row.number + ". " + row.note)[:500],
            created_by_id=user.id,
        )
        db.add(op)
        db.flush()
    updated_lines = [dict(l) for l in row.lines]
    for l in updated_lines:
        if l.get("stock_tracked", True):
            l["stock_unit_cost"] = str(dec(materials[l["material_id"]].purchase_price))
    for mid, q in quantities.items():
        m = materials[mid]
        m.quantity_in_stock = dec(m.quantity_in_stock) - q
        movement = stock.log_movement(
            db,
            m,
            -q,
            StockMovementReason.ISSUED_MANUAL,
            user,
            note=op.note,
            operation_id=op.id,
        )
        pending = {
            l["order_line"]: dec(l["quantity"])
            for l in row.lines
            if l.get("material_id") == mid
        }
        for hold in order_holds(db, order, material_id=mid, lock=True):
            index = hold.sales_line if hold.sales_order_id else hold.source_line
            if pending.get(index, 0) <= 0:
                continue
            take = min(pending[index], remaining(hold))
            pending[index] -= take
            hold.consumed_quantity = dec(hold.consumed_quantity) + take
            hold.version += 1
            db.add(
                ReservationEvent(
                    reservation_id=hold.id,
                    kind="issued",
                    quantity=take,
                    actor_id=user.id,
                    movement_id=movement.id,
                    note="Отгрузка " + row.number,
                    parameters={},
                )
            )
        db.flush()
        stock.sync_shortage_task(db, m)
    row.lines = updated_lines
    row.operation_id = op.id if op else None
    row.status = "posted"
    row.version += 1
    row.posted_at = datetime.now(timezone.utc)
    row.posted_by_id = user.id
    row.post_request_id = str(body.request_id)
    row.post_parameters = params
    order.version += 1
    event(db, order, user, "shipment_posted", {"operation_id": row.operation_id}, row)
    invalidate_cache(db)
    db.commit()
    return shipment_out(db, row, True)


def cancel_shipment(db, id, body, user):
    advisory(db, "sales-cancel:" + str(body.request_id))
    initial = get_shipment(db, id)
    order = get_order(db, initial.order_id, True) if initial.order_id else None
    row = get_shipment(db, id, True)
    if row.status == "posted" and (
        user.access_level(Module.WAREHOUSE) < AccessLevel.FULL
        or user.access_level(Module.CLIENTS) < AccessLevel.FULL
    ):
        fail(
            "Для отмены проведённой отгрузки нужен полный доступ к складу и клиентам",
            403,
        )
    params = body.model_dump(mode="json")
    if row.cancel_request_id == str(body.request_id):
        if row.cancelled_by_id != user.id or row.cancel_parameters != params:
            fail("Номер запроса уже использован с другими данными")
        return shipment_out(db, row, True)
    if (
        db.query(SalesShipment.id)
        .filter_by(cancel_request_id=str(body.request_id))
        .first()
    ):
        fail("Номер запроса уже использован для другой отмены")
    if (
        row.historical
        or row.archived
        or row.status == "cancelled"
        or row.version != body.version
    ):
        fail(
            "Исторический, изменённый, отменённый или архивный документ нельзя отменить. Обновите карточку."
        )
    if not body.note.strip():
        fail("Укажите причину отмены", 422)
    if order and order.archived:
        fail("Сначала верните заказ из архива")
    op = None
    if row.status == "posted":
        materials = validate_materials(db, row.lines, allow_archived=True)
        quantities, costs = defaultdict(Decimal), defaultdict(Decimal)
        for l in row.lines:
            if l.get("stock_tracked", True):
                mid = l["material_id"]
                q = dec(l["quantity"])
                quantities[mid] += q
                costs[mid] += q * dec(l["stock_unit_cost"])
        if quantities:
            op = WarehouseOperation(
                kind=WarehouseOperationKind.RECEIPT,
                occurred_at=datetime.now(timezone.utc),
                note=("Отмена отгрузки " + row.number + ": " + body.note)[:500],
                created_by_id=user.id,
            )
            db.add(op)
            db.flush()
        for mid, q in quantities.items():
            m = materials[mid]
            old = dec(m.quantity_in_stock)
            new = old + q
            if new >= Decimal("1000000000000"):
                fail("Количество превышает допустимый остаток", 422)
            m.purchase_price = (
                (old * dec(m.purchase_price) + costs[mid]) / new
                if old > 0
                else costs[mid] / q
            ).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
            m.quantity_in_stock = new
            stock.log_movement(
                db,
                m,
                q,
                StockMovementReason.RECEIPT,
                user,
                note=op.note,
                operation_id=op.id,
            )
            stock.sync_shortage_task(db, m)
        # A reversal returns physical goods; it does not recreate consumed holds.
    row.status = "cancelled"
    row.version += 1
    row.cancel_operation_id = op.id if op else None
    row.cancelled_by_id = user.id
    row.cancelled_at = datetime.now(timezone.utc)
    row.cancellation_note = body.note.strip()
    row.cancel_request_id = str(body.request_id)
    row.cancel_parameters = params
    if order:
        order.version += 1
    event(
        db,
        order,
        user,
        "shipment_cancelled",
        {"note": row.cancellation_note, "operation_id": row.cancel_operation_id},
        row,
    )
    invalidate_cache(db)
    db.commit()
    return shipment_out(db, row, True)


def archive_shipment(db, id, body, user):
    initial = get_shipment(db, id)
    order = get_order(db, initial.order_id, True) if initial.order_id else None
    row = get_shipment(db, id, True)
    if row.version != body.version or (body.archived and row.status == "draft"):
        fail("Сначала завершите черновик или обновите карточку отгрузки")
    row.archived = body.archived
    row.version += 1
    event(
        db,
        order,
        user,
        "shipment_archived" if body.archived else "shipment_restored",
        {},
        row,
    )
    db.commit()
    return shipment_out(db, row, True)
