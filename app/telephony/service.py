from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from fastapi import HTTPException
from app.clients.models import Client
from app.crm.models import CRMEntity
from app.telephony.models import PhoneCall, PhoneCallAudit
from app.core.config import settings
from app.telephony import routing, leads


def phone(value):
    return routing.normalize(value)


def match(db, row):
    if row.match_status == 'manual':
        match_deal(db, row)
        return
    if not row.phone:
        return
    clients = [c.id for c in db.query(Client).all() if phone(c.phone) == row.phone]
    entries = [e.id for e in db.query(CRMEntity).filter(
        CRMEntity.kind.in_(['contact', 'company', 'inquiry']), CRMEntity.archived.is_(False)
    ).all() if row.phone in routing.numbers(e)]
    # Shared family/company numbers require a human choice; never guess.
    if len(clients) > 1 or len(entries) > 1:
        row.client_id = row.crm_entity_id = None
        row.match_status = 'ambiguous'
    else:
        row.client_id = clients[0] if clients else None
        row.crm_entity_id = entries[0] if entries else None
        row.match_status = 'matched' if clients or entries else 'unmatched'
    match_deal(db, row)


def deal_parties(db, entry):
    ids = set(entry.payload.get('contact_ids') or [])
    if entry.payload.get('company_id'):
        ids.add(entry.payload['company_id'])
    parties = db.query(CRMEntity).filter(CRMEntity.id.in_(ids)).all() if ids else []
    numbers = set().union(*(routing.numbers(p) for p in parties))
    client_id = entry.payload.get('client_id')
    client = db.get(Client, client_id) if client_id else None
    if client:
        numbers.add(phone(client.phone))
    return ids, numbers - {''}, client_id


def match_deal(db, row):
    if row.deal_match_status in ('manual', 'inquiry'):
        return
    if row.match_status == 'ambiguous':
        row.deal_id, row.deal_match_status = None, 'ambiguous'
        return
    # All non-archived applications count, including won/lost ones: a shared
    # number cannot identify which application a historical conversation concerns.
    candidates = []
    for entry in db.query(CRMEntity).filter_by(kind='deal', archived=False):
        ids, numbers, client_id = deal_parties(db, entry)
        if (row.phone in numbers or row.crm_entity_id in ids
                or (client_id is not None and row.client_id == client_id)):
            candidates.append(entry.id)
    row.deal_id = candidates[0] if len(candidates) == 1 else None
    row.deal_match_status = 'matched' if len(candidates) == 1 else 'ambiguous' if candidates else 'unmatched'


def accept_inquiry(db, inquiry, deal, user):
    for row in db.query(PhoneCall).filter_by(crm_entity_id=inquiry.id):
        if row.deal_match_status != 'manual':
            before = row.deal_id
            row.deal_id, row.deal_match_status = deal.id, 'inquiry'
            audit(db, row, user, 'inquiry_accepted', {'inquiry_id': inquiry.id, 'before_deal_id': before, 'deal_id': deal.id})


def ingest(db, body, *, remember=True):
    cmd = body.get('cmd')
    if cmd not in ('history', 'event', 'contact', 'rating'):
        raise HTTPException(422, 'Неизвестная команда телефонии')
    if cmd == 'contact':
        return routing.contact(db, body)
    external_id = body.get('callid', '')
    number = phone(body.get('phone'))
    if cmd == 'event' and not external_id:
        # Provider live events can contain only phone/ext/type. The archive is
        # created by history with its stable callid; do not invent a call ID.
        if not number or body.get('type') not in ('INCOMING', 'ACCEPTED', 'COMPLETED', 'CANCELLED', 'OUTGOING'):
            raise HTTPException(422, 'Неверный номер или событие звонка')
        if remember:
            event = SimpleNamespace(phone=number,
                direction=body.get('direction') or ('out' if body['type'] == 'OUTGOING' else 'in'),
                employee=body.get('user') or body.get('ext') or '')
            leads.ensure(db, event, body)
            routing.observe_answer(db, event, body)
        db.commit()
        return {'result': 'ok'}
    if not external_id or len(external_id) > 255 or not 7 <= len(number) <= 15:
        raise HTTPException(422, 'Нужны идентификатор звонка и номер телефона')
    row = db.query(PhoneCall).filter_by(provider='megafon', external_id=external_id).with_for_update().first()
    if row is None:
        try:
            with db.begin_nested():
                direction = body.get('direction')
                if direction not in ('in', 'out'):
                    direction = body.get('type') if cmd == 'history' else ('out' if body.get('type') == 'OUTGOING' else 'in')
                row = PhoneCall(provider='megafon', external_id=external_id, phone=number,
                                direction=direction)
                db.add(row)
                db.flush()
        except IntegrityError:
            row = db.query(PhoneCall).filter_by(provider='megafon', external_id=external_id).with_for_update().one()
    if row.phone != number:
        raise HTTPException(409, 'Идентификатор звонка уже связан с другим номером')
    if cmd == 'history':
        if body.get('type') not in ('in', 'out'):
            raise HTTPException(422, 'Неверное направление звонка')
        try:
            duration = int(body.get('duration', 0))
            started = datetime.strptime(body['start'], '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc)
            if not 0 <= duration <= 86400:
                raise ValueError()
        except (KeyError, ValueError, TypeError):
            raise HTTPException(422, 'Неверное время или длительность звонка')
        row.direction, row.duration, row.started_at = body['type'], duration, started
        row.status = str(body.get('status', 'unknown'))[:40]
        url = str(body.get('link') or '')
        if url and len(url) <= 4096 and url != row.recording_url:
            row.recording_url = url
            if not row.audio_ready:
                row.processing, row.attempts, row.error = 'queued', 0, ''
    elif cmd == 'event' and row.started_at is None:
        row.status = str(body.get('type', 'unknown'))[:40]
        if body.get('direction') in ('in', 'out'):
            row.direction = body['direction']
    if cmd == 'history' or row.started_at is None:
        row.employee = str(body.get('user') or body.get('ext') or row.employee or '')[:255]
    if remember:
        leads.ensure(db, row, body)
    match(db, row)
    if remember:
        routing.observe_answer(db, row, body)
    db.commit()
    return {'result': 'ok'}


def get(db, id):
    row = db.get(PhoneCall, id)
    if not row:
        raise HTTPException(404, 'Звонок не найден')
    return row


def output(row):
    return {key: getattr(row, key) for key in (
        'id', 'phone', 'direction', 'employee', 'started_at', 'duration', 'status',
        'client_id', 'crm_entity_id', 'match_status', 'deal_id', 'deal_match_status', 'audio_ready', 'processing',
        'error', 'transcript', 'segments', 'analysis', 'reviewed_at', 'reviewed_by',
    )}


def listing(db, client_id=None, entity_id=None, offset=0, limit=50, unmatched=False, related=False):
    query = db.query(PhoneCall)
    if client_id:
        if not db.get(Client, client_id):
            raise HTTPException(404, 'Клиент не найден')
        query = query.filter(PhoneCall.client_id == client_id)
    if entity_id:
        entry = db.get(CRMEntity, entity_id)
        if not entry:
            raise HTTPException(404, 'Карточка не найдена')
        if entry.kind == 'deal':
            if related:
                ids, numbers, linked_client = deal_parties(db, entry)
                clauses = [PhoneCall.crm_entity_id.in_(ids), PhoneCall.phone.in_(numbers)]
                if linked_client:
                    clauses.append(PhoneCall.client_id == linked_client)
                query = query.filter(or_(*clauses), or_(PhoneCall.deal_id.is_(None), PhoneCall.deal_id != entry.id))
            else:
                query = query.filter(PhoneCall.deal_id == entry.id)
        else:
            clauses = [PhoneCall.crm_entity_id == entity_id]
            linked_client = entry.payload.get('client_id')
            if linked_client:
                clauses.append(PhoneCall.client_id == linked_client)
            query = query.filter(or_(*clauses))
    if unmatched:
        query = query.filter(or_(PhoneCall.match_status.in_(['unmatched', 'ambiguous']), PhoneCall.deal_match_status == 'ambiguous'))
    total = query.count()
    return {'total': total, 'items': [output(r) for r in query.order_by(
        PhoneCall.started_at.desc(), PhoneCall.id.desc()).offset(offset).limit(limit)]}


def audio_path(id):
    return Path(settings.storage_dir) / 'calls' / f'{int(id)}.audio'


def audit(db, row, user, action, details=None):
    db.add(PhoneCallAudit(call_id=row.id, actor_id=user.id, action=action, details=details or {}))
