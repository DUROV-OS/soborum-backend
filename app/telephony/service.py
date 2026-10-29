from datetime import datetime, timezone
from pathlib import Path
import re
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from fastapi import HTTPException
from app.clients.models import Client
from app.crm.models import CRMEntity
from app.telephony.models import PhoneCall, PhoneCallAudit
from app.core.config import settings


def phone(value):
    digits = re.sub(r'\D', '', str(value or ''))
    if len(digits) == 11 and digits.startswith('8'):
        digits = '7' + digits[1:]
    if len(digits) == 10:
        digits = '7' + digits
    return digits


def match(db, row):
    if row.match_status == 'manual' or not row.phone:
        return
    clients = [c.id for c in db.query(Client).all() if phone(c.phone) == row.phone]
    entries = [e.id for e in db.query(CRMEntity).filter(
        CRMEntity.kind.in_(['contact', 'company', 'inquiry']), CRMEntity.archived.is_(False)
    ).all() if phone(e.payload.get('phone')) == row.phone]
    # Shared family/company numbers require a human choice; never guess.
    if len(clients) > 1 or len(entries) > 1:
        row.client_id = row.crm_entity_id = None
        row.match_status = 'ambiguous'
    else:
        row.client_id = clients[0] if clients else None
        row.crm_entity_id = entries[0] if entries else None
        row.match_status = 'matched' if clients or entries else 'unmatched'


def ingest(db, body):
    cmd = body.get('cmd')
    if cmd not in ('history', 'event', 'contact', 'rating'):
        raise HTTPException(422, 'Неизвестная команда телефонии')
    external_id = body.get('callid', '')
    number = phone(body.get('phone'))
    if not external_id or len(external_id) > 255 or not 7 <= len(number) <= 15:
        raise HTTPException(422, 'Нужны идентификатор звонка и номер телефона')
    if cmd == 'contact':
        # Do not alter routing: no "responsible" in the response.
        matches = [c for c in db.query(Client).all() if phone(c.phone) == number]
        return {'contact_name': matches[0].full_name if len(matches) == 1 else ''}
    row = db.query(PhoneCall).filter_by(provider='megafon', external_id=external_id).with_for_update().first()
    if row is None:
        try:
            with db.begin_nested():
                row = PhoneCall(provider='megafon', external_id=external_id, phone=number,
                                direction=body.get('direction', body.get('type', 'in')))
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
    row.employee = str(body.get('user') or row.employee or '')[:255]
    match(db, row)
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
        'client_id', 'crm_entity_id', 'match_status', 'audio_ready', 'processing',
        'error', 'transcript', 'segments', 'analysis', 'reviewed_at', 'reviewed_by',
    )}


def listing(db, client_id=None, entity_id=None, offset=0, limit=50, unmatched=False):
    query = db.query(PhoneCall)
    if client_id:
        if not db.get(Client, client_id):
            raise HTTPException(404, 'Клиент не найден')
        query = query.filter(PhoneCall.client_id == client_id)
    if entity_id:
        entry = db.get(CRMEntity, entity_id)
        if not entry:
            raise HTTPException(404, 'Карточка не найдена')
        ids = {entity_id}
        linked_client = entry.payload.get('client_id')
        if entry.kind == 'deal':
            ids.update(entry.payload.get('contact_ids') or [])
            if entry.payload.get('company_id'):
                ids.add(entry.payload['company_id'])
        clauses = [PhoneCall.crm_entity_id.in_(ids)]
        if linked_client:
            clauses.append(PhoneCall.client_id == linked_client)
        query = query.filter(or_(*clauses))
    if unmatched:
        query = query.filter(PhoneCall.match_status.in_(['unmatched', 'ambiguous']))
    total = query.count()
    return {'total': total, 'items': [output(r) for r in query.order_by(
        PhoneCall.started_at.desc(), PhoneCall.id.desc()).offset(offset).limit(limit)]}


def audio_path(id):
    return Path(settings.storage_dir) / 'calls' / f'{int(id)}.audio'


def audit(db, row, user, action, details=None):
    db.add(PhoneCallAudit(call_id=row.id, actor_id=user.id, action=action, details=details or {}))
