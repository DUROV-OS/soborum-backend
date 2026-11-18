from datetime import datetime, timezone
import hmac
import hashlib
import logging
import time
from urllib.parse import parse_qsl
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session
from app.core.config import settings
from app.core.deps import require_view, require_edit, require_admin
from app.common.module_access import Module
from app.db.session import get_db
from app.users.models import User
from app.clients.models import Client
from app.crm.models import CRMEntity
from app.telephony import service
from app.telephony import routing, provider, connections
from uuid import UUID

router = APIRouter()
callback_log = logging.getLogger('uvicorn.error')
view = require_view(Module.CLIENTS)
edit = require_edit(Module.CLIENTS)


@router.get('/api/crm/communications/status')
def connection_status(db: Session = Depends(get_db), user: User = Depends(view)):
    return connections.status(db, user)


@router.post('/api/crm/communications/check')
def check_connections(user: User = Depends(require_admin)):
    return connections.check()


@router.get('/api/crm/telephony/routing')
def routing_config(db: Session = Depends(get_db), user: User = Depends(require_admin)):
    return routing.read_config(db)


@router.put('/api/crm/telephony/routing')
def save_routing(body: routing.Configuration, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    if body.auto_create_leads and len(settings.megafon_crm_token) < 32:
        raise HTTPException(422, 'Сначала настройте серверный ключ для событий ВАТС')
    if body.enabled and len(settings.megafon_crm_token) < 32:
        raise HTTPException(422, 'Сначала настройте серверный ключ для событий ВАТС')
    return routing.save_config(db, body, user)


class Dial(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: UUID
    phone: str = Field(min_length=10, max_length=40)


@router.post('/api/crm/calls/dial')
def dial(body: Dial, db: Session = Depends(get_db), user: User = Depends(edit)):
    return provider.dial(db, user, body.request_id, body.phone)


@router.post('/api/telephony/megafon')
async def webhook(request: Request, db: Session = Depends(get_db)):
    secret = settings.megafon_crm_token
    if len(secret) < 32:
        raise HTTPException(503, 'Телефония ещё не подключена')
    size = 0
    chunks = []
    async for chunk in request.stream():
        size += len(chunk)
        if size > 32768:
            raise HTTPException(413, 'Слишком большой запрос')
        chunks.append(chunk)
    try:
        pairs = parse_qsl(b''.join(chunks).decode('utf-8'), max_num_fields=50)
    except (UnicodeError, ValueError):
        raise HTTPException(400, 'Неверный формат')
    body = dict(pairs)
    if len(body) != len(pairs):
        raise HTTPException(400, 'Повторяющиеся параметры')
    supplied = body.pop('crm_token', '')
    if not hmac.compare_digest(supplied.encode(), secret.encode()):
        raise HTTPException(401, 'Неверный ключ телефонии')
    # Log delivery shape, never credentials, numbers, names or recording URLs.
    command = body.get('cmd') if body.get('cmd') in ('contact', 'event', 'history', 'rating') else 'unknown'
    call_ref = hashlib.sha256(body.get('callid', '').encode()).hexdigest()[:12] if body.get('callid') else '-'
    started = time.monotonic()
    callback_log.info('megafon_callback received cmd=%s call_ref=%s has_callid=%s has_diversion=%s',
                      command, call_ref, bool(body.get('callid')), bool(body.get('diversion')))
    status = 500
    try:
        result = await run_in_threadpool(service.ingest, db, body)
        status = 200
        return result
    except HTTPException as error:
        status = error.status_code
        raise
    finally:
        callback_log.info('megafon_callback completed cmd=%s call_ref=%s status=%s elapsed_ms=%s',
                          command, call_ref, status, round((time.monotonic() - started) * 1000))


@router.get('/api/crm/calls')
def calls(client_id: int | None = Query(None, ge=1), entity_id: int | None = Query(None, ge=1),
          unmatched: bool = False, related: bool = False, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100),
          db: Session = Depends(get_db), user: User = Depends(view)):
    return service.listing(db, client_id, entity_id, offset, limit, unmatched, related)


@router.get('/api/crm/calls/{id}/audio')
def audio(id: int, db: Session = Depends(get_db), user: User = Depends(view)):
    row = service.get(db, id)
    path = service.audio_path(id)
    if not row.audio_ready or not path.is_file():
        raise HTTPException(404, 'Запись ещё не получена')
    return FileResponse(path, media_type='audio/mpeg', headers={'Cache-Control': 'private, no-store'})


@router.post('/api/crm/calls/{id}/review')
def review(id: int, db: Session = Depends(get_db), user: User = Depends(edit)):
    row = service.get(db, id)
    if not row.audio_ready:
        raise HTTPException(409, 'Сначала дождитесь записи')
    row.reviewed_at, row.reviewed_by = datetime.now(timezone.utc), user.id
    service.audit(db, row, user, 'reviewed')
    db.commit()
    return service.output(row)


class Link(BaseModel):
    model_config = ConfigDict(extra='forbid')
    client_id: int | None = Field(None, ge=1)
    crm_entity_id: int | None = Field(None, ge=1)


@router.post('/api/crm/calls/{id}/link')
def link(id: int, body: Link, db: Session = Depends(get_db), user: User = Depends(edit)):
    row = service.get(db, id)
    if not body.client_id and not body.crm_entity_id:
        raise HTTPException(422, 'Выберите карточку')
    if body.client_id and not db.get(Client, body.client_id):
        raise HTTPException(404, 'Клиент не найден')
    if body.crm_entity_id:
        entry = db.get(CRMEntity, body.crm_entity_id)
        if not entry or entry.kind not in ('contact','company','deal','inquiry') or entry.archived:
            raise HTTPException(422, 'Нужна действующая карточка клиента или сделки')
    before = {'client_id': row.client_id, 'crm_entity_id': row.crm_entity_id, 'deal_id': row.deal_id}
    if body.crm_entity_id and entry.kind == 'deal':
        row.deal_id, row.deal_match_status = entry.id, 'manual'
    else:
        row.client_id, row.crm_entity_id, row.match_status = body.client_id, body.crm_entity_id, 'manual'
    service.audit(db, row, user, 'linked', {'before': before, 'after': body.model_dump()})
    service.record_event(db, row, actor_id=user.id)
    db.commit()
    return service.output(row)


@router.post('/api/crm/calls/{id}/retry')
def retry(id: int, db: Session = Depends(get_db), user: User = Depends(edit)):
    row = service.get(db, id)
    if row.processing == 'processing':
        raise HTTPException(409, 'Разговор уже обрабатывается')
    if not row.recording_url:
        raise HTTPException(409, 'МегаФон пока не передал запись')
    row.processing, row.attempts, row.error = 'queued', 0, ''
    row.reviewed_at = row.reviewed_by = None
    service.audit(db, row, user, 'retry')
    db.commit()
    return service.output(row)
