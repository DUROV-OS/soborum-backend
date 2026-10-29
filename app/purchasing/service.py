from collections import defaultdict
from decimal import Decimal
import hashlib
from fastapi import HTTPException
from sqlalchemy import text
from app.accounting.models import SupplierOrder, SupplierOrderStatus, MoneyMovement, MoneyMovementStatus
from app.accounting.schemas import SupplierOrderOut
from app.purchasing.models import PurchaseReceipt
from app.warehouse.models import (
    Supplier,
    SupplierStatus,
    WarehouseMaterial,
    WarehouseOperation,
    WarehouseOperationKind,
    StockMovement,
    StockMovementReason,
)


def dec(value):
    return Decimal(str(value or 0))


def balance(db, order):
    rows = db.query(MoneyMovement).filter_by(supply_id=order.id).all()
    paid = dec(order.opening_paid) + sum(
        (dec(m.amount) for m in rows if m.status == MoneyMovementStatus.POSTED), Decimal(0)
    )
    pending = sum(
        (
            dec(m.amount)
            for m in rows
            if m.status in (MoneyMovementStatus.DRAFT, MoneyMovementStatus.APPROVED)
        ),
        Decimal(0),
    )
    return {
        "paid": float(paid),
        "pending_payment": float(pending),
        "unpaid": float(max(Decimal(0), dec(order.total_cost) - paid)),
        "available_payment": float(max(Decimal(0), dec(order.total_cost) - paid - pending)),
    }


def out(db, order):
    return {
        **SupplierOrderOut.from_order(order).model_dump(mode="json"),
        "version": order.version,
        "archived": order.archived,
        "source_id": order.source_id,
        "number": order.number or str(order.id),
        "source_state": order.source_state,
        "opening_paid": order.opening_paid,
        **balance(db, order),
    }


def get_order(db, id, *, lock=False):
    query = db.query(SupplierOrder).filter_by(id=id)
    if lock:
        query = query.populate_existing().with_for_update(of=SupplierOrder)
    row = query.first()
    if row is None:
        raise HTTPException(404, "Заказ не найден")
    return row


def receipt_out(row):
    return {
        k: getattr(row, k)
        for k in [
            "id",
            "source_id",
            "request_id",
            "order_id",
            "supplier_id",
            "operation_id",
            "number",
            "status",
            "occurred_at",
            "lines",
            "note",
            "actor_id",
        ]
    }


def receive(db, body, user):
    parameters = body.model_dump(mode="json")
    if body.occurred_at.tzinfo is None:
        raise HTTPException(422, "Укажите дату приёмки с часовым поясом")
    # Serialize the same retry token even for standalone receipts using
    # different item lists, before acquiring order/material row locks.
    if db.get_bind().dialect.name == "postgresql":
        key = int.from_bytes(hashlib.sha256(str(body.request_id).encode()).digest()[:8], "big", signed=True)
        db.execute(text("select pg_advisory_xact_lock(:key)"), {"key": key})
    old = db.query(PurchaseReceipt).filter_by(request_id=str(body.request_id)).first()
    if old:
        if old.actor_id != user.id or old.parameters != parameters:
            raise HTTPException(409, "Этот номер запроса уже использован для другой приёмки")
        return receipt_out(old)
    order = get_order(db, body.order_id, lock=True) if body.order_id else None
    if order and (order.archived or body.version != order.version):
        raise HTTPException(409, "Заказ изменён или находится в архиве. Обновите карточку.")
    supplier_id = order.supplier_id if order else body.supplier_id
    supplier = db.get(Supplier, supplier_id) if supplier_id else None
    if supplier is None or supplier.status == SupplierStatus.ARCHIVED:
        raise HTTPException(422, "Выберите действующего поставщика")
    if order and body.supplier_id and body.supplier_id != order.supplier_id:
        raise HTTPException(422, "Поставщик не совпадает с заказом")
    items = [dict(item) for item in order.items] if order else []
    seen = set()
    planned = []
    for line in body.lines:
        item = None
        if order:
            if line.order_line is None or line.order_line >= len(items) or line.order_line in seen:
                raise HTTPException(422, "Укажите каждую позицию заказа один раз")
            seen.add(line.order_line)
            item = items[line.order_line]
            if dec(item.get("received_quantity")) + line.quantity > dec(item["quantity"]):
                raise HTTPException(409, "Приёмка превышает остаток по позиции «" + item["material"] + "»")
            expected = item.get("warehouse_material_id")
            if expected and line.material_id != expected:
                raise HTTPException(422, "Материал не совпадает с позицией заказа")
        tracked = item is None or item.get("stock_tracked", True)
        if tracked and not line.material_id:
            raise HTTPException(422, "Сопоставьте позицию со складской карточкой")
        if not order and line.order_line is not None:
            raise HTTPException(422, "Приёмка без заказа не содержит номера позиции заказа")
        planned.append((line, item, tracked))
    ids = sorted({line.material_id for line, item, tracked in planned if tracked})
    materials = {
        m.id: m
        for m in db.query(WarehouseMaterial)
        .filter(WarehouseMaterial.id.in_(ids))
        .order_by(WarehouseMaterial.id)
        .populate_existing()
        .with_for_update(of=WarehouseMaterial)
    }
    for id in ids:
        if id not in materials or materials[id].is_archived:
            raise HTTPException(422, "Выберите действующие складские карточки")
    if len({m.warehouse for m in materials.values()}) > 1:
        raise HTTPException(422, 'Одна приёмка относится к одному складу. Оформите поставки на разные склады отдельно.')
    quantities = defaultdict(Decimal)
    values = defaultdict(Decimal)
    result = []
    for line, item, tracked in planned:
        material = materials[line.material_id] if tracked else None
        if material and not material.is_fractional and line.quantity % 1:
            raise HTTPException(422, "Материал «" + material.title + "» учитывается целыми единицами")
        if tracked:
            quantities[material.id] += line.quantity
            values[material.id] += line.quantity * line.unit_price
        if item is not None:
            item["received_quantity"] = float(dec(item.get("received_quantity")) + line.quantity)
            if tracked:
                item["warehouse_material_id"] = material.id
        result.append(
            {
                "order_line": line.order_line,
                "material_id": line.material_id,
                "title": material.title if material else item["material"],
                "unit": material.unit if material else item.get("unit", ""),
                "quantity": float(line.quantity),
                "unit_price": float(line.unit_price),
            }
        )
    operation = None
    if ids:
        operation = WarehouseOperation(
            kind=WarehouseOperationKind.RECEIPT,
            occurred_at=body.occurred_at,
            created_by_id=user.id,
            note=(
                ("Приёмка заказа №" + str(order.id) if order else "Приёмка от " + supplier.name)
                + ("; " + body.note if body.note else "")
            )[:500],
        )
        db.add(operation)
        db.flush()
        for id in ids:
            material = materials[id]
            prior = dec(material.quantity_in_stock)
            new = prior + quantities[id]
            old_price = dec(material.purchase_price)
            if new >= Decimal("1000000000000"):
                raise HTTPException(422, "Количество превышает допустимый остаток")
            material.purchase_price = (
                (prior * old_price + values[id]) / new if prior > 0 else values[id] / quantities[id]
            ).quantize(Decimal("0.00000001"))
            material.quantity_in_stock = new
            db.add(
                StockMovement(
                    warehouse_material_id=id,
                    delta=quantities[id],
                    reason=StockMovementReason.RECEIPT,
                    operation_id=operation.id,
                    balance_after=new,
                    created_by_id=user.id,
                    note="Приёмка закупки",
                )
            )
    if order:
        order.items = items
        order.version += 1
        complete = all(dec(i.get("received_quantity")) >= dec(i["quantity"]) for i in items)
        order.status = SupplierOrderStatus.RECEIVED if complete else SupplierOrderStatus.IN_TRANSIT
        if complete:
            order.received_at = body.occurred_at
    row = PurchaseReceipt(
        request_id=str(body.request_id),
        order_id=order.id if order else None,
        supplier_id=supplier_id,
        operation_id=operation.id if operation else None,
        actor_id=user.id,
        number="",
        status="posted",
        occurred_at=body.occurred_at,
        lines=result,
        note=body.note,
        parameters=parameters,
    )
    db.add(row)
    db.flush()
    row.number = str(row.id)
    from app.warehouse.service import sync_shortage_task

    for material in materials.values():
        sync_shortage_task(db, material)
    db.commit()
    db.refresh(row)
    return receipt_out(row)
