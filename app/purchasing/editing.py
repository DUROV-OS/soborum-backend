"""Explicit procurement edits. Posted stock is corrected through its receipt."""
from copy import deepcopy
from decimal import Decimal
from uuid import uuid4
from fastapi import HTTPException
from app.accounting.models import SupplierOrderStatus, MoneyMovement, MoneyMovementStatus
from app.purchasing.models import PurchaseReceipt
from app.purchasing.control_models import PurchaseControlRecord
from app.purchasing.service import dec


def fail(message, code=409):
    raise HTTPException(code, message)


def order_snapshot(row):
    return {'number': row.number, 'supplier_id': row.supplier_id, 'organization_id': row.organization_id, 'items': deepcopy(row.items),
            'total_cost': str(row.total_cost), 'expected_at': str(row.expected_at) if row.expected_at else None,
            'comment': row.comment, 'status': row.status.value, 'version': row.version}


def has_receipts(db, row):
    return bool(any(dec(i.get('received_quantity')) for i in row.items) or db.query(PurchaseReceipt.id).filter_by(order_id=row.id).first())


def received_by_line(db, order):
    totals = [Decimal(0) for _ in order.items]
    ambiguous = set()
    for receipt in db.query(PurchaseReceipt).filter_by(order_id=order.id, status='posted'):
        for line in receipt.lines:
            index = line.get('order_line')
            if index is None:
                matches = [i for i, item in enumerate(order.items) if
                    (line.get('material_id') and item.get('warehouse_material_id') == line['material_id']) or
                    (not line.get('material_id') and item.get('material') == line.get('title'))]
                if len(matches) != 1:
                    ambiguous.update(matches or range(len(order.items)))
                    continue
                index = matches[0]
            if 0 <= index < len(totals): totals[index] += dec(line['quantity'])
    return [max(totals[i], dec(item.get('received_quantity'))) for i, item in enumerate(order.items)], ambiguous


def apply_order(db, row, body):
    from app.accounting.service import _validate_order_items, _compute_order_total, require_supplier_order_organization
    from app.purchasing.service import balance
    from app.warehouse.models import Supplier, SupplierStatus
    payload = body.model_dump(exclude_unset=True)
    has_history = has_receipts(db, row)
    if body.organization_id is not None and body.organization_id != row.organization_id:
        if row.source_id or has_history or row.opening_paid or db.query(MoneyMovement.id).filter_by(supply_id=row.id).first():
            fail('Заказ связан с документами. Изменение организации требует их сверки.')
        require_supplier_order_organization(db, body.organization_id)
        row.organization_id = body.organization_id
        db.expire(row, ['organization'])
    if body.supplier_id is not None and body.supplier_id != row.supplier_id:
        if has_history or row.opening_paid or db.query(MoneyMovement.id).filter_by(supply_id=row.id).first():
            fail('У заказа есть приёмки или платежи. Для другого поставщика создайте отдельный заказ.')
        supplier = db.get(Supplier, body.supplier_id)
        if not supplier or supplier.status == SupplierStatus.ARCHIVED:
            fail('Выберите действующего поставщика', 422)
        row.supplier_id = supplier.id
    if 'number' in payload:
        if not body.number or not body.number.strip(): fail('Укажите номер заказа', 422)
        row.number = body.number.strip()
    if 'items' in payload:
        if body.items is None: fail('Укажите позиции заказа', 422)
        _validate_order_items(body.items)
        updated = [item.model_dump() for item in body.items]
        if has_history:
            actual, ambiguous = received_by_line(db, row)
            if len(updated) < len(row.items):
                fail('Строки связаны с приёмками: сохраните их порядок. Количество можно уменьшить до уже принятого.')
            for index, old in enumerate(row.items):
                item = updated[index]
                for key in ('warehouse_material_id', 'unit', 'stock_tracked'):
                    if item.get(key) != old.get(key, True if key == 'stock_tracked' else '' if key == 'unit' else None):
                        fail('Нельзя заменить товар или единицу в строке, связанной с приёмкой. Добавьте новую позицию.')
                if dec(item['received_quantity']) != dec(old.get('received_quantity')):
                    fail('Фактически принятое количество меняется в документе приёмки', 422)
                if index in ambiguous and dec(item['quantity']) < dec(old['quantity']):
                    fail('В исходных приёмках есть неоднозначные строки. Сначала сопоставьте их с заказом.')
                if dec(item['quantity']) < actual[index]:
                    fail('Количество заказа не может быть меньше уже принятого. Сначала исправьте приёмку.')
                item['received_quantity'] = float(actual[index])
            if any(dec(i['received_quantity']) for i in updated[len(row.items):]):
                fail('Новая строка не может содержать принятое количество', 422)
        elif any(dec(i['received_quantity']) for i in updated):
            fail('Фактически принятое количество заполняется приёмками', 422)
        new_total = _compute_order_total(body.items)
        current = balance(db, row)
        remaining = max(Decimal(0), dec(new_total) - dec(current['return_credit']) - dec(current['paid']))
        if dec(current['pending_payment']) > remaining:
            fail('Новая сумма меньше открытых заявок на оплату. Сначала уменьшите или отмените эти заявки.')
        if dec(new_total) < dec(current['return_credit']):
            fail('Сумма заказа не может быть меньше оформленных возвратов поставщику.')
        row.items = updated
        row.total_cost = new_total
        if has_history:
            complete = all(dec(i.get('received_quantity')) >= dec(i['quantity']) for i in updated)
            row.status = SupplierOrderStatus.RECEIVED if complete else SupplierOrderStatus.IN_TRANSIT
            if not complete: row.received_at = None
    if 'expected_at' in payload: row.expected_at = body.expected_at
    if 'comment' in payload: row.comment = body.comment


def record_order_edit(db, row, before, body, actor_id):
    if actor_id:
        db.add(PurchaseControlRecord(supplier_id=row.supplier_id, order_id=row.id, kind='order_edit',
            request_id=str(uuid4()), actor_id=actor_id, content={'reason': body.reason.strip(), 'before': before, 'after': order_snapshot(row)}))
