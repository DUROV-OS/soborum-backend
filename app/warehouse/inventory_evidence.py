"""A count is backed by an immutable, personally confirmed scan and snapshot."""
from datetime import timezone
from typing import Literal
from uuid import UUID
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session
from app.common.module_access import Module
from app.core.deps import require_edit, require_view
from app.db.session import get_db
from app.costing.technical_basis import fingerprint
from app.warehouse.control_models import WarehouseInventoryAct as Act
from app.warehouse import controls as c

router = APIRouter(prefix='/inventory-evidence', tags=['Акты инвентаризации'])


class Attach(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    request_id: UUID
    version: int = Field(ge=1)
    file_id: int = Field(gt=0)
    locator: str = Field(min_length=3, max_length=500)
    confirmed: Literal[True]


def snapshot(row):
    return {'document_id': row.id, 'number': row.number, 'warehouse': row.warehouse,
            'occurred_at': (row.occurred_at.replace(tzinfo=timezone.utc) if row.occurred_at.tzinfo is None else row.occurred_at.astimezone(timezone.utc)).isoformat(), 'note': row.note,
            'version': row.version, 'lines': row.lines}


def posted_facts(row):
    data = snapshot(row)
    data.pop('version')
    return {**data, 'status': row.status, 'operation_ids': row.operation_ids}


def movements_match(db, act):
    from app.warehouse.models import StockMovement, StockMovementReason
    expected={line['material_id']: c.D(line['delta']) for line in act.content['posted_lines'] if c.D(line['delta'])}
    movements=db.query(StockMovement).filter(StockMovement.operation_id.in_(act.content['operation_ids'])).all()
    return len(movements)==len(expected) and all(m.warehouse_material_id in expected
        and c.D(str(m.delta))==expected[m.warehouse_material_id]
        and m.reason==(StockMovementReason.COUNT_GAIN if m.delta>0 else StockMovementReason.COUNT_LOSS)
        and m.created_by_id==act.actor_id for m in movements) and len({m.warehouse_material_id for m in movements})==len(expected)


def act_out(db, act, row):
    from app.users.models import User
    actor = db.get(User, act.actor_id)
    from app.warehouse.electronic_inventory import observations_intact
    intact = c.proof_intact(db, act.content['proof']) and observations_intact(db,act.content)
    return {'id': act.id, 'stage': act.stage, 'actor_id': act.actor_id,
            'actor_name': actor.full_name if actor else 'Сотрудник №' + str(act.actor_id), 'created_at': (act.created_at.replace(tzinfo=timezone.utc) if act.created_at.tzinfo is None else act.created_at), 'mode':act.content.get('mode','scan'), 'proof': act.content['proof'], 'intact': intact,
            'current': intact and (act.content.get('posted_hash') == fingerprint(posted_facts(row)) and movements_match(db,act) if act.stage == 'posted' else act.content['source_hash'] == fingerprint(snapshot(row))),
            'snapshot': act.content['snapshot'], 'operation_ids': act.content.get('operation_ids', [])}


def attach(db, row, body, user):
    from app.warehouse.documents import draft, fail
    old = db.query(Act).filter_by(request_id=str(body.request_id)).first()
    if old:
        if old.stage != 'prepared' or old.document_id != row.id or old.actor_id != user.id or old.content['request'] != body.model_dump(mode='json'):
            fail('Ключ подтверждения уже использован с другими данными')
        return old
    draft(row, body.version)
    if row.kind != 'inventory': fail('Акт пересчёта относится только к инвентаризации')
    if not row.lines or any(line.get('quantity') is None for line in row.lines) or not row.note.strip():
        fail('Заполните фактическое количество каждой позиции и основание пересчёта', 422)
    source = snapshot(row)
    act = Act(document_id=row.id, stage='prepared', request_id=str(body.request_id), actor_id=user.id,
              file_id=body.file_id, content={'request': body.model_dump(mode='json'), 'snapshot': source,
                  'source_hash': fingerprint(source), 'proof': c.document(db, body.file_id, body.locator)})
    db.add(act); db.flush()
    return act


def require_act(db, row, body):
    from app.warehouse.documents import fail
    if not body.act_id or not body.confirmed:
        fail('Подтвердите электронный акт или приложите акт пересчёта', 422)
    if db.query(Act).filter_by(request_id=str(body.request_id)).first():
        fail('Этот ключ уже использован для подтверждения акта')
    act = db.get(Act, body.act_id)
    if not act or act.stage != 'prepared' or act.document_id != row.id:
        fail('Акт не относится к этой инвентаризации')
    state = act_out(db, act, row)
    if not state['current']:
        fail('Документ или результаты пересчёта изменились. Повторно сверьте и подтвердите акт.')
    return act


def posted(db, row, body, user, prepared):
    db.add(Act(document_id=row.id, stage='posted', request_id=str(body.request_id), actor_id=user.id,
        file_id=prepared.file_id, content={'request': body.model_dump(mode='json'), 'prepared_id': prepared.id,
            'prepared_by_id': prepared.actor_id, 'mode':prepared.content.get('mode','scan'),
            'observations':prepared.content.get('observations',[]), 'proof': prepared.content['proof'],
            'source_hash': prepared.content['source_hash'], 'snapshot': prepared.content['snapshot'],
            'posted_lines': row.lines, 'posted_hash': fingerprint(posted_facts(row)), 'operation_ids': row.operation_ids}))
    db.flush()


def check_replay(db, row, body, user):
    from app.warehouse.documents import fail
    act = db.query(Act).filter_by(document_id=row.id, stage='posted').first()
    # Older inventory documents remain readable; a retry never creates a new posting.
    if act and (act.actor_id != user.id or act.content['request'] != body.model_dump(mode='json')):
        fail('Этот ключ проведения уже использован с другим актом или подтверждением')


@router.get('/{document_id}')
def history(document_id: int, db: Session = Depends(get_db), user=Depends(require_view(Module.WAREHOUSE))):
    from app.warehouse.documents import get
    row = get(db, document_id)
    return [act_out(db, a, row) for a in db.query(Act).filter_by(document_id=row.id).order_by(Act.id.desc())]


@router.post('/{document_id}', status_code=201)
def add(document_id: int, body: Attach, db: Session = Depends(get_db), user=Depends(require_edit(Module.WAREHOUSE))):
    from app.warehouse.documents import get, advisory
    advisory(db, 'inventory-evidence:' + str(body.request_id))
    row = get(db, document_id, True)
    act = attach(db, row, body, user)
    db.commit()
    return act_out(db, act, row)
