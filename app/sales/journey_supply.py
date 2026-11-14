"""Explicit purchasing allocations. The existing immutable sales journal is the ledger.

An allocation is planning evidence, never a payment, receipt or stock reservation.
Stale allocations still occupy capacity until explicitly released.
"""
from collections import defaultdict
from decimal import Decimal as D
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.accounting.models import SupplierOrder
from app.common.module_access import Module, AccessLevel
from app.core.deps import require_view
from app.costing.integration import fingerprint, link_out
from app.costing.models import CostingOrderLink
from app.costing.technical_basis import fingerprint as digest
from app.db.session import get_db
from app.moysklad.recipes import Recipe
from app.purchasing.models import PurchaseReceipt
from app.purchasing.returns import returned_lines
from app.sales.models import SalesEvent
from app.sales.service import fail, get_order, event
from app.warehouse.documents import advisory
from app.warehouse.models import WarehouseMaterial
from app.warehouse.reservation_service import dec

router = APIRouter()
view = require_view(Module.CLIENTS)


def can_allocate(user):
    return any(user.access_level(m) >= AccessLevel.EDIT for m in (Module.ACCOUNTING, Module.WAREHOUSE))


def needs(db, order, index):
    if index < 0 or index >= len(order.lines):
        fail('Строка заказа не найдена', 404)
    line = order.lines[index]
    link = db.query(CostingOrderLink).filter_by(order_id=order.id, line_index=index).first()
    recipe = db.get(Recipe, link.recipe_id) if link and link.recipe_id else None
    result = defaultdict(D)
    # A dedicated order norm expresses the WHOLE order line, not one product.
    if recipe:
        for item in recipe.content.get('inputs', []):
            if item.get('material_id'):
                result[item['material_id']] += dec(item['quantity'])
    elif line.get('stock_tracked', True) and line.get('material_id'):
        result[line['material_id']] = dec(line['quantity'])
    return result, link, recipe


def active_links(db):
    rows = db.query(SalesEvent).filter(SalesEvent.kind.in_(['supply_allocated', 'supply_released'])).order_by(SalesEvent.id).all()
    removed = {r.data['allocation_id'] for r in rows if r.kind == 'supply_released'}
    return [r for r in rows if r.kind == 'supply_allocated' and r.id not in removed]


def supply_basis(order, index):
    item = order.items[index]
    # Receiving changes quantities/status, not the commercial basis of an allocation.
    return digest({'supplier_id': order.supplier_id, 'currency': order.currency,
                   'item': {k: item.get(k) for k in ('warehouse_material_id', 'material', 'quantity', 'unit_price', 'unit', 'stock_tracked')}})


def target_basis(order, index, recipe):
    return digest({'line': fingerprint(order, index), 'recipe': None if recipe is None else
                   {'id': recipe.id, 'version': recipe.version, 'content': recipe.content}})


def state(db, row, order, recipe):
    data = row.data
    supply = db.get(SupplierOrder, data['supplier_order_id'])
    index = data['supplier_line']
    fresh = bool(supply and not supply.archived and index < len(supply.items)
                 and supply_basis(supply, index) == data['supply_basis']
                 and data['order_line'] < len(order.lines)
                 and target_basis(order, data['order_line'], recipe) == data['target_basis'])
    # Show receipt evidence for the supplier line separately. It does not reserve
    # or assign received lots to a particular customer.
    receipts = []
    for receipt in db.query(PurchaseReceipt).filter_by(order_id=data['supplier_order_id'], status='posted').order_by(PurchaseReceipt.id):
        returns = returned_lines(db, receipt.id)
        quantity = sum((max(D(0), dec(l['quantity']) - returns[i]) for i, l in enumerate(receipt.lines)
                        if l.get('order_line') == index and l.get('material_id') == data['material_id']), D(0))
        if quantity:
            receipts.append({'id': receipt.id, 'number': receipt.number, 'quantity': str(quantity),
                             'local_stock_posted': receipt.operation_id is not None})
    material = db.get(WarehouseMaterial, data['material_id'])
    return {'id': row.id, 'order_line': data['order_line'], 'material_id': data['material_id'],
            'title': material.title if material else f'Материал №{data["material_id"]}', 'unit': material.unit if material else '',
            'supplier_order_id': data['supplier_order_id'], 'supplier_line': index,
            'number': (supply.number or str(supply.id)) if supply else str(data['supplier_order_id']),
            'quantity': data['quantity'], 'fresh': fresh, 'created_at': row.created_at,
            'receipts': receipts, 'expected_at': supply.expected_at if supply else None}


class Allocation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: UUID
    version: int = Field(ge=1)
    order_line: int = Field(ge=0, le=499)
    supplier_order_id: int = Field(gt=0)
    supplier_version: int = Field(ge=1)
    supplier_line: int = Field(ge=0, le=499)
    quantity: D = Field(gt=0, le=10000000, max_digits=17, decimal_places=6, allow_inf_nan=False)


class Release(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: UUID
    version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=500)


def replay(db, order_id, user, body, kind):
    old = db.query(SalesEvent).filter_by(request_id=str(body.request_id)).first()
    if old and (old.order_id != order_id or old.actor_id != user.id or old.kind != kind
                or old.data['request'] != body.model_dump(mode='json')):
        fail('Номер запроса использован для другого действия')
    return old


def lock(db, user):
    if not can_allocate(user):
        fail('Закрепление закупки доступно сотруднику склада или бухгалтерии с правом изменения', 403)
    advisory(db, 'sales-supply-allocations')


@router.get('/orders/{id}/supply-options')
def options(id: int, order_line: int, db: Session = Depends(get_db), user=Depends(view)):
    if not can_allocate(user):
        fail('Нет права закреплять закупки', 403)
    order = get_order(db, id)
    if order_line < 0:
        fail('Неверная строка заказа', 422)
    demand, _, _ = needs(db, order, order_line)
    allocations = active_links(db)
    result = []
    for supply in db.query(SupplierOrder).filter_by(archived=False).order_by(SupplierOrder.id.desc()):
        for index, item in enumerate(supply.items):
            mid = item.get('warehouse_material_id')
            material = db.get(WarehouseMaterial, mid) if mid else None
            if mid not in demand or not item.get('stock_tracked', True) or not material or material.is_archived or item.get('unit') != material.unit:
                continue
            assigned = sum((dec(a.data['quantity']) for a in allocations if a.data['supplier_order_id'] == supply.id and a.data['supplier_line'] == index), D(0))
            target_assigned = sum((dec(a.data['quantity']) for a in allocations if a.order_id == id and a.data['order_line'] == order_line and a.data['material_id'] == mid), D(0))
            capacity = max(D(0), min(dec(item['quantity']) - assigned, demand[mid] - target_assigned))
            if capacity:
                result.append({'supplier_order_id': supply.id, 'supplier_line': index, 'supplier_version': supply.version,
                               'number': supply.number or str(supply.id), 'material_id': mid, 'title': item['material'],
                               'unit': material.unit, 'available_quantity': str(capacity), 'expected_at': supply.expected_at})
    return result


@router.post('/orders/{id}/supply-allocations')
def allocate(id: int, body: Allocation, db: Session = Depends(get_db), user=Depends(view)):
    lock(db, user)
    old = replay(db, id, user, body, 'supply_allocated')
    if old:
        return {'id': old.id}
    from app.costing.service import lock as cost_lock
    cost_lock(db)
    order = get_order(db, id, True)
    if order.version != body.version or order.status != 'confirmed' or order.archived:
        fail('Для закрепления нужен актуальный подтверждённый заказ')
    demand, link, recipe = needs(db, order, body.order_line)
    from app.sales.service import shipped
    if shipped(db, order).get(body.order_line, D(0)) >= dec(order.lines[body.order_line]['quantity']):
        fail('Строка заказа уже полностью отгружена')
    if recipe and (recipe.archived or recipe.content.get('warnings')):
        fail('Производственный норматив требует сверки')
    if not link or not link_out(db, link, order)['fresh']:
        fail('Сначала закрепите утверждённую смету за строкой заказа')
    from app.costing.governance import assert_approved
    from app.costing.models import CostingEstimate
    supply = db.query(SupplierOrder).filter_by(id=body.supplier_order_id).populate_existing().with_for_update().first()
    if not supply or supply.archived or supply.version != body.supplier_version or body.supplier_line >= len(supply.items):
        fail('Закупка изменена или недоступна. Обновите список.')
    # Match receiving's supplier -> material lock order.
    assert_approved(db, db.get(CostingEstimate, link.estimate_id))
    item = supply.items[body.supplier_line]
    mid = item.get('warehouse_material_id')
    material = db.get(WarehouseMaterial, mid) if mid else None
    if mid not in demand or not item.get('stock_tracked', True) or not material or material.is_archived or item.get('unit') != material.unit:
        fail('Позиция закупки должна точно совпадать с изделием или материалом действующего норматива')
    if not material.is_fractional and body.quantity % 1:
        fail('Материал учитывается целыми единицами', 422)
    current = active_links(db)
    assigned = sum((dec(a.data['quantity']) for a in current if a.data['supplier_order_id'] == supply.id and a.data['supplier_line'] == body.supplier_line), D(0))
    target_assigned = sum((dec(a.data['quantity']) for a in current if a.order_id == id and a.data['order_line'] == body.order_line and a.data['material_id'] == mid), D(0))
    if assigned + body.quantity > dec(item['quantity']) or target_assigned + body.quantity > demand[mid]:
        fail('Количество превышает свободную часть закупки или потребность заказа; проверьте другие закрепления')
    event(db, order, user, 'supply_allocated', {'request': body.model_dump(mode='json'), 'order_line': body.order_line,
          'supplier_order_id': supply.id, 'supplier_line': body.supplier_line, 'material_id': mid, 'quantity': str(body.quantity),
          'supply_basis': supply_basis(supply, body.supplier_line), 'target_basis': target_basis(order, body.order_line, recipe)}, request_id=body.request_id)
    order.version += 1
    db.flush()
    saved = db.query(SalesEvent).filter_by(request_id=str(body.request_id)).one()
    db.commit()
    return {'id': saved.id}


@router.post('/orders/{id}/supply-allocations/{allocation_id}/release')
def release(id: int, allocation_id: int, body: Release, db: Session = Depends(get_db), user=Depends(view)):
    lock(db, user)
    old = replay(db, id, user, body, 'supply_released')
    if old:
        if old.data['allocation_id'] != allocation_id:
            fail('Номер запроса использован для другого закрепления')
        return {'id': old.id}
    order = get_order(db, id, True)
    if order.version != body.version:
        fail('Заказ изменился. Обновите маршрут.')
    row = next((r for r in active_links(db) if r.id == allocation_id and r.order_id == id), None)
    if not row:
        fail('Действующее закрепление не найдено', 404)
    if not body.reason.strip():
        fail('Укажите причину снятия закрепления', 422)
    event(db, order, user, 'supply_released', {'allocation_id': row.id, 'reason': body.reason.strip(),
          'request': body.model_dump(mode='json')}, request_id=body.request_id)
    order.version += 1
    db.commit()
    return {'released': True}
