"""Versioned, evidenced receipt corrections without replaying the original receipt."""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from uuid import UUID
from fastapi import APIRouter, Depends
from pydantic import Field
from sqlalchemy.orm import Session
from app.accounting.evidence_schemas import Strict, Citation
from app.accounting.models import SupplierOrderStatus
from app.common.module_access import Module
from app.core.deps import require_edit, require_view
from app.db.session import get_db
from app.purchasing.control_models import PurchaseControlRecord as Record
from app.purchasing.models import PurchaseReturn
from app.purchasing.returns import receipt, receipt_summary, fail, tracked, aware
from app.purchasing.service import dec, get_order
from app.users.models import User
from app.warehouse.models import StockMovement, WarehouseOperation, WarehouseOperationKind, StockMovementReason
from app.warehouse.documents import advisory
from app.warehouse.service import _lock_materials, log_movement, sync_shortage_task, resolve_occurred_at

router = APIRouter()


def snapshot(row):
    return {'number': row.number, 'occurred_at': aware(row.occurred_at).astimezone(timezone.utc).isoformat(),
            'note': row.note, 'lines': deepcopy(row.lines), 'status': row.status}


def token(row):
    return hashlib.sha256(json.dumps(snapshot(row), sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


class Line(Strict):
    quantity: Decimal = Field(gt=0, max_digits=18, decimal_places=6, allow_inf_nan=False)
    unit_price: Decimal = Field(ge=0, max_digits=18, decimal_places=6, allow_inf_nan=False)


class Correction(Strict):
    request_id: UUID
    edit_token: str = Field(min_length=64, max_length=64)
    number: str = Field(min_length=1, max_length=255)
    occurred_at: datetime
    note: str = Field(default='', max_length=5000)
    reason: str = Field(min_length=3, max_length=1000)
    lines: list[Line] = Field(min_length=1, max_length=500)
    evidence: Citation | None = None


@router.get('/receipts/{id}/history')
def history(id: int, db: Session = Depends(get_db), user: User = Depends(require_view(Module.ACCOUNTING))):
    base = receipt(db, id)
    return [{'id': r.id, 'created_at': r.created_at, 'actor_id': r.actor_id, 'content': r.content}
            for r in db.query(Record).filter_by(kind='receipt_edit', supplier_id=base.supplier_id).order_by(Record.id.desc())
            if r.content.get('receipt_id') == id]


def correct(db, id, body, user):
    from app.purchasing import controls
    from app.purchasing.editing import received_by_line
    if body.occurred_at.tzinfo is None:
        fail('Укажите дату с часовым поясом', 422)
    resolve_occurred_at(body.occurred_at)
    parameters = body.model_dump(mode='json')
    advisory(db, 'purchase-receipt-edit:' + str(body.request_id))
    prior = db.query(Record).filter_by(request_id=str(body.request_id)).first()
    if prior:
        if prior.kind != 'receipt_edit' or prior.actor_id != user.id or prior.content.get('receipt_id') != id or prior.content['request'] != parameters:
            fail('Номер запроса уже использован для другого действия')
        return receipt_summary(db, receipt(db, id))
    initial = receipt(db, id)
    order = get_order(db, initial.order_id, lock=True) if initial.order_id else None
    base = receipt(db, id, lock=True)
    if not base.supplier_id:
        fail('Сначала сопоставьте поставщика этой приёмки с рабочим справочником')
    if base.status != 'posted' or (order and order.archived):
        fail('Для изменения нужна действующая приёмка и заказ вне архива')
    if token(base) != body.edit_token:
        fail('Приёмка изменена другим сотрудником. Откройте её заново.')
    if len(body.lines) != len(base.lines):
        fail('Сохраните строки приёмки и их порядок. Новые товары оформляйте дополнительной приёмкой.', 422)
    returns = db.query(PurchaseReturn).filter_by(receipt_id=id).all()
    if any(aware(r.occurred_at) < body.occurred_at for r in returns):
        fail('Дата приёмки не может быть позже связанного возврата')
    changed = [i for i, (old, new) in enumerate(zip(base.lines, body.lines))
               if dec(old['quantity']) != new.quantity or dec(old['unit_price']) != new.unit_price]
    before = snapshot(base)
    proof = {}
    stock_changes = []
    lines = deepcopy(base.lines)
    if changed:
        require_edit(Module.WAREHOUSE)(user)
        if not body.evidence or not base.supplier_id:
            fail('Изменение количества или цены необходимо подтвердить документом поставщика', 422)
        proof = controls.proofs(db, base.supplier_id, [body.evidence])
        if returns:
            fail('У приёмки есть возвраты. Изменение чисел требует сверки возвратов и связанных платежей.')
        actual, ambiguous = received_by_line(db, order) if order else ([], set())
        deltas = {}
        values = {}
        for i in changed:
            old, new = lines[i], body.lines[i]
            difference = new.quantity - dec(old['quantity'])
            if order:
                index = old.get('order_line')
                if index is None:
                    matches = [j for j, item in enumerate(order.items) if
                               (old.get('material_id') and item.get('warehouse_material_id') == old['material_id']) or
                               (not old.get('material_id') and item.get('material') == old.get('title'))]
                    index = matches[0] if len(matches) == 1 else None
                if index is None or index in ambiguous or not 0 <= index < len(actual):
                    fail('Сначала сопоставьте строку исходной приёмки со строкой заказа')
                actual[index] += difference
                if actual[index] < 0 or actual[index] > dec(order.items[index]['quantity']):
                    fail('Количество приёмок превышает заказ. Сначала увеличьте количество в заказе.')
            if tracked(db, base, old):
                mid = old.get('material_id')
                if not mid:
                    fail('Сначала сопоставьте товар со складской карточкой')
                if not base.operation_id:
                    fail('Эта приёмка перенесена вместе с начальным остатком. Для изменения чисел нужна сверка начального остатка и себестоимости; реквизиты можно изменить сейчас.')
                deltas[mid] = deltas.get(mid, Decimal(0)) + difference
                values[mid] = values.get(mid, Decimal(0)) + new.quantity * new.unit_price - dec(old['quantity']) * dec(old['unit_price'])
            old.update(quantity=str(new.quantity), unit_price=str(new.unit_price))
        materials = _lock_materials(db, list(deltas))
        from app.warehouse.reservation_service import assert_available
        for mid, delta in sorted(deltas.items()):
            material = materials.get(mid)
            if not material or material.is_archived:
                fail('Товар не найден или находится в архиве')
            first = db.query(StockMovement).filter_by(operation_id=base.operation_id, warehouse_material_id=mid).order_by(StockMovement.id).first()
            if not first or db.query(StockMovement.id).filter(StockMovement.warehouse_material_id == mid, StockMovement.id > first.id, StockMovement.delta < 0).first():
                fail('После этой приёмки товар уже расходовался. Нужен перерасчёт связанных выдач и себестоимости; реквизиты можно изменить сейчас.')
            if not material.is_fractional and delta % 1:
                fail('Товар учитывается целыми единицами', 422)
            if any(line.get('material_id') == mid and line.get('unit') and line['unit'] != material.unit for line in lines):
                fail('Единица товара отличается от приёмки')
            quantity = dec(material.quantity_in_stock) + delta
            value = dec(material.quantity_in_stock) * dec(material.purchase_price) + values[mid]
            if quantity <= 0 or value < 0 or quantity >= Decimal('1e12') or value >= Decimal('1e18'):
                fail('Корректировка несовместима с текущим количеством или стоимостью остатка')
            if delta < 0:
                assert_available(db, [(material, -delta)])
            stock_changes.append({'material_id': mid, 'quantity_before': str(material.quantity_in_stock),
                                  'price_before': str(material.purchase_price), 'delta': str(delta),
                                  'value_delta': str(values[mid]), 'quantity_after': str(quantity),
                                  'price_after': str((value / quantity).quantize(Decimal('0.00000001')))})
        for change in stock_changes:
            material = materials[change['material_id']]
            delta = dec(change['delta'])
            op = WarehouseOperation(kind=WarehouseOperationKind.COUNT_GAIN if delta >= 0 else WarehouseOperationKind.COUNT_LOSS,
                                    occurred_at=datetime.now(timezone.utc), created_by_id=user.id,
                                    note=('Корректировка приёмки № ' + base.number + '. ' + body.reason)[:500])
            db.add(op); db.flush()
            material.quantity_in_stock = dec(change['quantity_after'])
            material.purchase_price = dec(change['price_after'])
            db.flush()
            if delta:
                log_movement(db, material, delta, StockMovementReason.COUNT_GAIN if delta > 0 else StockMovementReason.COUNT_LOSS,
                             user, note=op.note, operation_id=op.id)
            change['operation_id'] = op.id
            sync_shortage_task(db, material)
        if order:
            items = deepcopy(order.items)
            for i, quantity in enumerate(actual): items[i]['received_quantity'] = float(quantity)
            order.items = items
            complete = all(dec(i.get('received_quantity')) >= dec(i['quantity']) for i in items)
            order.status = SupplierOrderStatus.RECEIVED if complete else SupplierOrderStatus.IN_TRANSIT
            order.received_at = body.occurred_at if complete else None
    base.number, base.note, base.occurred_at, base.lines = body.number, body.note, body.occurred_at, lines
    if order: order.version += 1
    db.add(Record(kind='receipt_edit', supplier_id=base.supplier_id, order_id=base.order_id, actor_id=user.id,
                  request_id=str(body.request_id), content={'receipt_id': id, 'request': parameters, 'before': before,
                  'after': snapshot(base), 'proofs': proof, 'stock_changes': stock_changes}))
    db.commit()
    return receipt_summary(db, base)


@router.patch('/receipts/{id}')
def update(id: int, body: Correction, db: Session = Depends(get_db), user: User = Depends(require_edit(Module.ACCOUNTING))):
    try:
        return correct(db, id, body, user)
    except Exception:
        db.rollback()
        raise
