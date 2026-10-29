"""Posted supplier returns, receipt limits and separately approved cash refunds."""

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func
from sqlalchemy.orm import Session
from app.accounting.models import (
    SupplierOrder,
    MoneyMovement,
    MoneyMovementStatus,
    MoneySubkind,
    BankAccount,
)
from app.accounting.schemas import MoneyMovementCreate, MoneyMovementOut
from app.common.module_access import Module
from app.core.deps import require_view, require_edit, require_full
from app.db.session import get_db
from app.purchasing.models import PurchaseReceipt, PurchaseReturn
from app.purchasing.service import receipt_out, dec, get_order
from app.purchasing.schemas import Payment, Archive
from app.users.models import User
from app.warehouse.models import (
    WarehouseMaterial,
    WarehouseOperation,
    WarehouseOperationKind,
    StockMovementReason,
    Supplier,
    SupplierStatus,
)
from app.warehouse.service import (
    _lock_materials,
    log_movement,
    sync_shortage_task,
    recalculate_supplier_totals,
    resolve_occurred_at,
)
from app.warehouse.documents import advisory

router = APIRouter()
view, edit, full = (
    require_view(Module.ACCOUNTING),
    require_edit(Module.ACCOUNTING),
    require_full(Module.ACCOUNTING),
)


class ReturnLine(BaseModel):
    model_config = ConfigDict(extra="forbid")
    receipt_line: int = Field(ge=0)
    material_id: int | None = Field(default=None, ge=1)
    quantity: Decimal = Field(
        gt=0, max_digits=18, decimal_places=6, allow_inf_nan=False
    )


class ReturnCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    receipt_id: int = Field(ge=1)
    occurred_at: datetime
    note: str = Field(min_length=1, max_length=500)
    lines: list[ReturnLine] = Field(min_length=1, max_length=500)


class CancelReturn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    version: int = Field(ge=1)
    note: str = Field(min_length=1, max_length=500)


def fail(message, code=409):
    raise HTTPException(code, message)


def aware(value):
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def receipt(db, id, lock=False):
    query = db.query(PurchaseReceipt).filter_by(id=id)
    if lock:
        query = query.populate_existing().with_for_update(of=PurchaseReceipt)
    row = query.first()
    if row is None:
        fail("Приёмка не найдена", 404)
    return row


def returned_lines(db, receipt_id):
    result = defaultdict(Decimal)
    for doc in db.query(PurchaseReturn).filter_by(
        receipt_id=receipt_id, cancelled=False
    ):
        for line in doc.lines:
            result[line["receipt_line"]] += dec(line["quantity"])
    return result


def tracked(db, base, line):
    if "stock_tracked" in line:
        return bool(line["stock_tracked"])
    if line.get("material_id"):
        return True
    index = line.get("order_line")
    order = db.get(SupplierOrder, base.order_id) if base.order_id else None
    if order and index is not None and 0 <= index < len(order.items):
        return order.items[index].get("stock_tracked", True)
    return True


def receipt_summary(db, base):
    prior = returned_lines(db, base.id)
    return {
        **receipt_out(base),
        "lines": [
            {
                **line,
                "receipt_line": i,
                "stock_tracked": tracked(db, base, line),
                "returned_quantity": float(prior[i]),
                "returnable_quantity": float(
                    max(Decimal(0), dec(line["quantity"]) - prior[i])
                ),
            }
            for i, line in enumerate(base.lines)
        ],
    }


def order_credit(db, order_id):
    return dec(
        db.query(func.sum(PurchaseReturn.amount))
        .filter_by(order_id=order_id, cancelled=False)
        .scalar()
    )


def order_refunds(db, order_id, statuses=(MoneyMovementStatus.POSTED,)):
    return dec(
        db.query(func.sum(MoneyMovement.amount))
        .join(PurchaseReturn, MoneyMovement.purchase_return_id == PurchaseReturn.id)
        .filter(PurchaseReturn.order_id == order_id, MoneyMovement.status.in_(statuses))
        .scalar()
    )


def refund_totals(db, row):
    movements = (
        db.query(MoneyMovement)
        .filter_by(purchase_return_id=row.id)
        .order_by(MoneyMovement.id)
        .all()
    )
    posted = sum(
        (dec(m.amount) for m in movements if m.status == MoneyMovementStatus.POSTED),
        Decimal(0),
    )
    pending = sum(
        (
            dec(m.amount)
            for m in movements
            if m.status in (MoneyMovementStatus.DRAFT, MoneyMovementStatus.APPROVED)
        ),
        Decimal(0),
    )
    return {
        "refunded": float(posted),
        "refund_pending": float(pending),
        "refund_available": (
            0
            if row.cancelled
            else float(max(Decimal(0), dec(row.amount) - posted - pending))
        ),
        "refunds": [
            {
                "id": m.id,
                "amount": m.amount,
                "status": m.status.value,
                "account_id": m.account_id,
            }
            for m in movements
        ],
    }


def out(db, row):
    base = receipt(db, row.receipt_id)
    return {
        **{
            name: getattr(row, name)
            for name in (
                "id",
                "receipt_id",
                "order_id",
                "supplier_id",
                "operation_id",
                "actor_id",
                "number",
                "amount",
                "currency",
                "archived",
                "cancelled",
                "cancellation_note",
                "cancelled_at",
                "version",
                "occurred_at",
                "lines",
                "note",
            )
        },
        "receipt_number": base.number,
        **refund_totals(db, row),
    }


def validate_refund(
    db,
    return_id,
    amount,
    currency,
    account_id,
    subkind,
    affects_profit,
    exclude_id=None,
):
    if return_id is None:
        return
    original = db.get(PurchaseReturn, return_id)
    if original is None:
        fail("Возврат поставщику не найден", 422)
    if original.order_id:
        get_order(db, original.order_id, lock=True)
    row = (
        db.query(PurchaseReturn)
        .filter_by(id=return_id)
        .populate_existing()
        .with_for_update(of=PurchaseReturn)
        .one()
    )
    if row.archived and exclude_id is None:
        fail("Возврат находится в архиве")
    if row.cancelled:
        fail("Возврат отменён")
    if subkind != MoneySubkind.OTHER_INCOME or affects_profit is not False:
        fail(
            "Возврат денег оформляется как прочее поступление без влияния на прибыль",
            422,
        )
    account = db.get(BankAccount, account_id)
    if not account or currency != row.currency or account.currency != row.currency:
        fail("Валюта возврата и банковского счёта должна совпадать", 422)
    query = db.query(MoneyMovement).filter(
        MoneyMovement.purchase_return_id == return_id,
        MoneyMovement.status != MoneyMovementStatus.CANCELLED,
    )
    if exclude_id is not None:
        query = query.filter(MoneyMovement.id != exclude_id)
    used = sum((dec(m.amount) for m in query), Decimal(0))
    if dec(amount) + used > dec(row.amount):
        fail("Сумма превышает остаток возврата с учётом проводок на согласовании")


@router.get("/receipts/{id}")
def receipt_detail(id: int, db: Session = Depends(get_db), user: User = Depends(view)):
    return receipt_summary(db, receipt(db, id))


@router.get("/returns")
def listing(
    archive: str = Query("active", pattern="^(active|archived|all)$"),
    receipt_id: int | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(view),
):
    query = db.query(PurchaseReturn)
    if archive != "all":
        query = query.filter_by(archived=archive == "archived")
    if receipt_id is not None:
        query = query.filter_by(receipt_id=receipt_id)
    return {
        "total": query.count(),
        "items": [
            out(db, r)
            for r in query.order_by(
                PurchaseReturn.occurred_at.desc(), PurchaseReturn.id.desc()
            )
            .offset(offset)
            .limit(limit)
        ],
    }


@router.get("/returns/{id}")
def detail(id: int, db: Session = Depends(get_db), user: User = Depends(view)):
    row = db.get(PurchaseReturn, id)
    if row is None:
        fail("Возврат не найден", 404)
    return out(db, row)


@router.post("/returns", status_code=201)
def create(
    body: ReturnCreate, db: Session = Depends(get_db), user: User = Depends(edit)
):
    require_edit(Module.WAREHOUSE)(user)
    if body.occurred_at.tzinfo is None:
        fail("Укажите дату с часовым поясом", 422)
    resolve_occurred_at(body.occurred_at)
    if not body.note.strip():
        fail("Укажите причину возврата", 422)
    parameters = body.model_dump(mode="json")
    advisory(db, "purchase-return:" + str(body.request_id))
    old = db.query(PurchaseReturn).filter_by(request_id=str(body.request_id)).first()
    if old:
        if old.actor_id != user.id or old.parameters != parameters:
            fail("Номер запроса уже использован с другими данными")
        return out(db, old)
    initial = receipt(db, body.receipt_id)
    order = get_order(db, initial.order_id, lock=True) if initial.order_id else None
    base = receipt(db, body.receipt_id, lock=True)
    supplier = db.get(Supplier, base.supplier_id)
    if (
        base.status != "posted"
        or not supplier
        or supplier.status == SupplierStatus.ARCHIVED
    ):
        fail("Нужна проведённая приёмка действующего поставщика")
    if aware(body.occurred_at) < aware(base.occurred_at):
        fail("Возврат не может быть раньше приёмки", 422)
    returned = returned_lines(db, base.id)
    seen = set()
    plans = []
    for line in body.lines:
        if line.receipt_line >= len(base.lines) or line.receipt_line in seen:
            fail("Укажите каждую строку приёмки один раз", 422)
        seen.add(line.receipt_line)
        source = base.lines[line.receipt_line]
        if returned[line.receipt_line] + line.quantity > dec(source["quantity"]):
            fail("Возврат превышает полученное количество по строке")
        stock = tracked(db, base, source)
        mid = source.get("material_id")
        if mid and line.material_id not in (None, mid):
            fail("Материал не совпадает с приёмкой", 422)
        mid = mid or line.material_id
        if stock and not mid:
            fail("Сопоставьте материал со складской карточкой", 422)
        if not stock and line.material_id:
            fail("Услуга не списывает складской материал", 422)
        plans.append((line, source, mid if stock else None))
    materials = _lock_materials(db, [mid for _, _, mid in plans if mid])
    quantities = defaultdict(Decimal)
    lines = []
    amount = Decimal(0)
    for line, source, mid in plans:
        material = materials.get(mid) if mid else None
        if mid and (not material or material.is_archived):
            fail("Выберите действующий материал", 422)
        if material and source.get("unit") and material.unit != source["unit"]:
            fail("Единица материала отличается от приёмки", 422)
        if material and not material.is_fractional and line.quantity % 1:
            fail("Материал учитывается целыми единицами", 422)
        if mid:
            quantities[mid] += line.quantity
        price = dec(source["unit_price"])
        amount += price * line.quantity
        lines.append(
            {
                "receipt_line": line.receipt_line,
                "material_id": mid,
                "title": source["title"],
                "unit": source.get("unit", ""),
                "quantity": str(line.quantity),
                "unit_price": str(price),
                "stock_cost": str(dec(material.purchase_price)) if material else None,
            }
        )
    for mid, q in quantities.items():
        if dec(materials[mid].quantity_in_stock) < q:
            fail("Недостаточно материала на складе для возврата")
    from app.warehouse.reservation_service import assert_available

    assert_available(db, [(materials[mid], q) for mid, q in quantities.items()])
    amount = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if order:
        from app.purchasing.service import balance

        current = balance(db, order)
        if amount > dec(current["net_total"]):
            fail("Сумма возврата превышает стоимость заказа после предыдущих возвратов")
        if dec(current["pending_payment"]) > max(
            Decimal(0), dec(current["net_total"]) - amount - dec(current["paid"])
        ):
            fail("Сначала уменьшите или отмените платежи по заказу на согласовании")
    row = PurchaseReturn(
        request_id=str(body.request_id),
        parameters=parameters,
        receipt_id=base.id,
        order_id=base.order_id,
        supplier_id=base.supplier_id,
        actor_id=user.id,
        number="",
        amount=amount,
        currency=order.currency if order else "RUB",
        occurred_at=body.occurred_at,
        lines=lines,
        note=body.note.strip(),
        archived=False,
        version=1,
    )
    db.add(row)
    db.flush()
    row.number = "ВП-" + str(row.id)
    if quantities:
        op = WarehouseOperation(
            kind=WarehouseOperationKind.PURCHASE_RETURN,
            occurred_at=body.occurred_at,
            note=(
                "Возврат № "
                + row.number
                + " по приёмке № "
                + base.number
                + ". "
                + row.note
            )[:500],
            created_by_id=user.id,
        )
        db.add(op)
        db.flush()
        row.operation_id = op.id
        for mid, q in quantities.items():
            material = materials[mid]
            material.quantity_in_stock = dec(material.quantity_in_stock) - q
            db.flush()
            log_movement(
                db,
                material,
                -q,
                StockMovementReason.PURCHASE_RETURN,
                user,
                note=op.note,
                operation_id=op.id,
            )
            sync_shortage_task(db, material)
    if order:
        order.version += 1
        recalculate_supplier_totals(db, row.supplier_id)
    db.commit()
    return out(db, row)


@router.post("/returns/{id}/archive")
def archive_return(
    id: int, body: Archive, db: Session = Depends(get_db), user: User = Depends(full)
):
    row = (
        db.query(PurchaseReturn)
        .filter_by(id=id)
        .populate_existing()
        .with_for_update(of=PurchaseReturn)
        .first()
    )
    if row is None:
        fail("Возврат не найден", 404)
    if row.version != body.version:
        fail("Документ изменён. Откройте его заново.")
    row.archived = body.archived
    row.version += 1
    db.commit()
    return out(db, row)


@router.post("/returns/{id}/refunds", status_code=201)
def refund(
    id: int, body: Payment, db: Session = Depends(get_db), user: User = Depends(edit)
):
    from app.accounting.service import create_money_movement

    row = db.get(PurchaseReturn, id)
    if row is None:
        fail("Возврат не найден", 404)
    from app.accounting.models import Counterparty

    counterparty = db.query(Counterparty).filter_by(supplier_id=row.supplier_id).first()
    movement = create_money_movement(
        db,
        MoneyMovementCreate(
            subkind=MoneySubkind.OTHER_INCOME,
            amount=float(body.amount),
            currency=row.currency,
            account_id=body.account_id,
            purchase_return_id=id,
            counterparty_id=counterparty.id if counterparty else None,
            affects_profit=False,
            payment_purpose="Возврат денег от поставщика по документу № " + row.number,
        ),
        user.id,
        request_id=str(body.request_id),
    )
    return MoneyMovementOut.from_movement(movement)


@router.post("/returns/{id}/cancel")
def cancel(
    id: int,
    body: CancelReturn,
    db: Session = Depends(get_db),
    user: User = Depends(full),
):
    require_full(Module.WAREHOUSE)(user)
    reason = body.note.strip()
    if not reason:
        fail("Укажите причину отмены", 422)
    advisory(db, "cancel-purchase-return:" + str(body.request_id))
    other = (
        db.query(PurchaseReturn)
        .filter_by(cancel_request_id=str(body.request_id))
        .first()
    )
    if other and other.id != id:
        fail("Номер запроса уже использован")
    initial = db.get(PurchaseReturn, id)
    if not initial:
        fail("Возврат не найден", 404)
    order = get_order(db, initial.order_id, lock=True) if initial.order_id else None
    receipt(db, initial.receipt_id, lock=True)
    row = (
        db.query(PurchaseReturn)
        .filter_by(id=id)
        .populate_existing()
        .with_for_update(of=PurchaseReturn)
        .one()
    )
    if (
        row.cancelled
        and row.cancel_request_id == str(body.request_id)
        and row.cancelled_by_id == user.id
        and row.cancellation_note == reason
    ):
        return out(db, row)
    if row.cancelled or row.archived or row.version != body.version:
        fail("Возврат изменён, отменён или находится в архиве")
    if (
        db.query(MoneyMovement.id)
        .filter(
            MoneyMovement.purchase_return_id == id,
            MoneyMovement.status != MoneyMovementStatus.CANCELLED,
        )
        .first()
    ):
        fail("Сначала отмените связанные денежные проводки")
    quantities = defaultdict(Decimal)
    values = defaultdict(Decimal)
    for line in row.lines:
        mid = line.get("material_id")
        if mid:
            quantities[mid] += dec(line["quantity"])
            values[mid] += dec(line["quantity"]) * dec(line["stock_cost"])
    materials = _lock_materials(db, list(quantities))
    for line in row.lines:
        material = materials.get(line.get("material_id"))
        if material and (
            material.unit != line["unit"]
            or (not material.is_fractional and dec(line["quantity"]) % 1)
        ):
            fail(
                "Единица учёта материала изменилась. Проверьте карточку перед отменой."
            )
    for mid, q in quantities.items():
        m = materials.get(mid)
        if not m or m.is_archived:
            fail("Разархивируйте складские карточки перед отменой")
        if dec(m.quantity_in_stock) < 0 or dec(m.quantity_in_stock) + q >= Decimal(
            "1000000000000"
        ):
            fail("Проверьте остаток склада перед отменой")
    now = datetime.now(timezone.utc)
    if quantities:
        op = WarehouseOperation(
            kind=WarehouseOperationKind.RECEIPT,
            occurred_at=now,
            note=("Отмена возврата № " + row.number + ". " + reason)[:500],
            created_by_id=user.id,
        )
        db.add(op)
        db.flush()
        row.cancel_operation_id = op.id
        for mid, q in quantities.items():
            m = materials[mid]
            old = dec(m.quantity_in_stock)
            m.purchase_price = (
                (old * dec(m.purchase_price) + values[mid]) / (old + q)
            ).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
            m.quantity_in_stock = old + q
            db.flush()
            log_movement(
                db,
                m,
                q,
                StockMovementReason.RECEIPT,
                user,
                note=op.note,
                operation_id=op.id,
            )
            sync_shortage_task(db, m)
    row.cancelled = True
    row.cancel_request_id = str(body.request_id)
    row.cancelled_at = now
    row.cancelled_by_id = user.id
    row.cancellation_note = reason
    row.version += 1
    if order:
        order.version += 1
        recalculate_supplier_totals(db, row.supplier_id)
    db.commit()
    return out(db, row)
