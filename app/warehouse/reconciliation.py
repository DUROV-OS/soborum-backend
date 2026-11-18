"""Documented attribution of historical issues. No inventory or norms are rewritten."""
from datetime import timezone
from decimal import Decimal as D
from typing import Literal
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session
from app.common.module_access import Module, AccessLevel
from app.core.deps import require_edit
from app.db.session import get_db
from app.costing.governance import require_person, responsible
from app.costing.technical_basis import fingerprint
from app.production.locks import lock_house_materials
from app.production.models import MaterialRequest
from app.warehouse import controls as c, service as s
from app.warehouse.control_models import WarehouseControlRecord as Record
from app.warehouse.models import StockMovement, StockMovementReason
from app.warehouse.control_router import reader

router = APIRouter(prefix='/control/reconciliation', tags=['Сверка старых выдач'])


class Allocation(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    block_material_id: int = Field(gt=0)
    quantity: D = Field(gt=0, le=1000000000, decimal_places=6, allow_inf_nan=False)
    locator: str = Field(min_length=3, max_length=500)


class Confirm(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    request_id: UUID
    source_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    file_id: int = Field(gt=0)
    reason: str = Field(min_length=3, max_length=500)
    confirmed: Literal[True]
    allocations: list[Allocation] = Field(min_length=1, max_length=5000)


def stamp(value):
    return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)).isoformat()


def movement_facts(m):
    op = m.operation
    return {'movement_id': m.id, 'material_id': m.warehouse_material_id, 'delta': c.number(m.delta),
            'reason': m.reason.value, 'created_at': stamp(m.created_at), 'actor_id': m.created_by_id,
            'operation_id': m.operation_id, 'production_id': op.production_id if op else None,
            'occurred_at': stamp(op.occurred_at) if op else None,
            'unit': m.warehouse_material.unit, 'fractional': m.warehouse_material.is_fractional}


def latest(db, movement_id):
    return next((r for r in db.query(Record).filter_by(kind='reconcile').order_by(Record.id.desc())
                 if r.content['movement_id'] == movement_id), None)


def evidence(db, movement):
    row = latest(db, movement.id)
    if not row: return None
    intact = c.proof_intact(db, row.content['proof']) and row.content['movement'] == movement_facts(movement)
    return {'record_id': row.id, 'proof': row.content['proof'], 'intact': bool(intact),
            'actor_id': row.actor_id, 'allocations': row.content['allocations']}


def resolved(db, movement):
    info = evidence(db, movement)
    if not info: return None
    if not info['intact']: c.fail('Документ сверки старой выдачи повреждён или исходная выдача изменилась. Требуется проверка.', 409)
    row = latest(db, movement.id)
    return row.production_id, row.content['allocations'][0]['blocks']


def eligible(db, movement):
    if not movement or movement.reason != StockMovementReason.ISSUED_TECHCARD or D(str(movement.delta)) >= 0 or not movement.operation or not movement.operation.production_id:
        c.fail('Выберите старую выдачу по техкарте с точной ссылкой на производство', 404)
    for row in db.query(Record).filter(Record.kind.in_(['issue', 'request_issue'])):
        if any(a['movement_id'] == movement.id for a in row.content['allocations']):
            c.fail('У этой выдачи уже есть исходное распределение. Переписывать его через сверку нельзя.', 409)
    return movement.operation.production_id


def plan(db, movement):
    pid = eligible(db, movement)
    blocks = [b for b in s._house_block_materials(db, pid) if b.warehouse_material_id == movement.warehouse_material_id]
    known = {}
    for row in db.query(Record).filter_by(production_id=pid).filter(Record.kind.in_(['issue', 'request_issue', 'reconcile'])).order_by(Record.id):
        for allocation in row.content['allocations']:
            known[allocation['movement_id']] = allocation['blocks']
    # Older request issues already identify a single block and consume that block's capacity.
    legacy = db.query(StockMovement).filter_by(reason=StockMovementReason.ISSUED, warehouse_material_id=movement.warehouse_material_id).filter(StockMovement.delta < 0).all()
    for old in legacy:
        req = db.get(MaterialRequest, old.reference_id)
        if old.id not in known and req and req.block_material.block.production_id == pid:
            known[old.id] = [{'block_material_id': req.block_material_id, 'quantity': c.number(-D(str(old.delta)))}]
    restored = {}
    for row in db.query(Record).filter_by(production_id=pid, kind='return'):
        for item in row.content['restored']:
            key = (row.content['movement_id'], item['block_material_id'])
            restored[key] = restored.get(key, D(0)) + D(item['quantity'])
    used = {}
    for mid, allocations in known.items():
        if mid == movement.id: continue
        for item in allocations:
            bid = item['block_material_id']
            used[bid] = used.get(bid, D(0)) + D(item['quantity']) - restored.get((mid, bid), D(0))
    previous = latest(db, movement.id)
    lines = [{'block_material_id': b.id, 'block_id': b.block_id, 'block_name': b.block.name,
              'unit': b.unit, 'provided': c.number(b.quantity_provided),
              'attributed_elsewhere': c.number(used.get(b.id, D(0))),
              'available': c.number(D(str(b.quantity_provided)) - used.get(b.id, D(0)))} for b in blocks]
    source = {'movement': movement_facts(movement), 'blocks': lines,
              'previous_record_id': previous.id if previous else None,
              'returns': [r.id for r in c.returns_for(db, movement.id)]}
    return {'movement_id': movement.id, 'production_id': pid, 'house': s.house_label(s.get_production_or_404(db, pid)),
            'material': movement.warehouse_material.title, 'unit': movement.warehouse_material.unit,
            'quantity': c.number(-D(str(movement.delta))), 'source_hash': fingerprint(source), 'source': source,
            'blocks': lines, 'previous': evidence(db, movement),
            'can_change': not source['returns'],
            'blocked_reason': 'По выдаче уже были возвраты; прежнее распределение сохраняется для проверки истории.' if source['returns'] else None}


def confirm(db, movement_id, body, user):
    c.lock(db)
    movement = db.get(StockMovement, movement_id)
    pid = eligible(db, movement)
    old = c.replay(db, 'reconcile', pid, body, user)
    if old:
        if old.content['movement_id'] != movement_id: c.fail('Ключ относится к другой выдаче', 409)
        return old
    require_person(db, user, 'production')
    lock_house_materials(db, pid)
    s._lock_materials(db, [movement.warehouse_material_id])
    state = plan(db, movement)
    if not state['can_change']: c.fail(state['blocked_reason'], 409)
    if state['source_hash'] != body.source_hash: c.fail('Данные выдачи или блоков изменились. Обновите сверку.', 409)
    ids = [a.block_material_id for a in body.allocations]
    if len(ids) != len(set(ids)) or sum((a.quantity for a in body.allocations), D(0)) != -D(str(movement.delta)):
        c.fail('Распределите ровно количество исходной выдачи, без повторения блоков', 422)
    available = {line['block_material_id']: line for line in state['blocks']}
    for a in body.allocations:
        block = available.get(a.block_material_id)
        if not block or block['unit'] != state['unit'] or a.quantity > D(block['available']):
            c.fail('Количество превышает ещё не распределённую выдачу блока или не совпадают единицы учёта', 409)
        if not movement.warehouse_material.is_fractional and a.quantity != a.quantity.to_integral_value():
            c.fail('Материал учитывается в целых единицах', 422)
    proof = c.document(db, body.file_id, 'Ссылки на строки документа приведены у каждого блока')
    return c.record(db, 'reconcile', pid, body, user, movement_id=movement.id,
        movement=state['source']['movement'], source=state['source'], proof=proof,
        replaces=state['source']['previous_record_id'],
        allocations=[{'movement_id': movement.id, 'blocks': [
            {'block_material_id': a.block_material_id, 'quantity': c.number(a.quantity), 'locator': a.locator}
            for a in body.allocations]}])


@router.get('')
def listing(production_id: int | None = Query(None, gt=0), db: Session = Depends(get_db), user=Depends(reader)):
    movements = db.query(StockMovement).filter_by(reason=StockMovementReason.ISSUED_TECHCARD).filter(StockMovement.delta < 0).order_by(StockMovement.id.desc())
    rows = []
    for movement in movements:
        if production_id and (not movement.operation or movement.operation.production_id != production_id): continue
        try: rows.append(plan(db, movement))
        except HTTPException as exc:
            if exc.status_code not in (404, 409): raise
    person = responsible(db, 'production')
    return {'items': rows, 'responsible': {'id': person.id, 'name': person.full_name} if person else None,
            'can_confirm': bool(person and person.id == user.id and user.access_level(Module.PRODUCTION) >= AccessLevel.EDIT)}


@router.post('/{movement_id}')
def apply(movement_id: int, body: Confirm, db: Session = Depends(get_db), user=Depends(require_edit(Module.PRODUCTION))):
    row = confirm(db, movement_id, body, user)
    db.commit()
    return {'record_id': row.id, 'stock_changed': False}
