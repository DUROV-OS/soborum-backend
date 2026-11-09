import asyncio
import contextlib
from anyio import CancelScope
from pathlib import Path
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import update
from sqlalchemy.orm import Session
from app.core.config import settings
from app.core.deps import require_edit, require_admin, require_view
from app.core.security import decode_access_token
from app.common.module_access import Module
from app.db.session import get_db
from app.users.models import User
from app.telephony import browser_phone as phone
from app.telephony.models import BrowserPhoneAccount, BrowserPhoneCall, PhoneCall
from app.telephony.voice_gateway import Gateway

router = APIRouter(prefix='/api/crm')


def director(user: User = Depends(require_admin)):
    if user.job_role != 'director':
        raise HTTPException(403, 'Подключение браузерной телефонии доступно руководителю')
    return user


@router.get('/browser-phone/status')
def status(db: Session = Depends(get_db), user: User = Depends(require_view(Module.CLIENTS))):
    return phone.status(db, user)


@router.get('/browser-phone/accounts')
def accounts(db: Session = Depends(get_db), user: User = Depends(director)):
    return {'items': phone.accounts(db), 'gateway_configured': phone.configured(), 'domain': settings.browser_phone_domain}


class Account(BaseModel):
    model_config = ConfigDict(extra='forbid')
    login: str = Field(min_length=1, max_length=100)
    password: SecretStr = SecretStr('')
    enabled: bool = True


@router.put('/browser-phone/accounts/{user_id}')
def account(user_id: int, body: Account, db: Session = Depends(get_db), user: User = Depends(director)):
    return {'items': phone.save_account(db, user, user_id, body.login.strip(), body.password.get_secret_value(), body.enabled)}


@router.post('/browser-phone/accounts/{user_id}/check')
async def check_account(user_id: int, db: Session = Depends(get_db), user: User = Depends(director)):
    target, account = db.get(User, user_id), db.get(BrowserPhoneAccount, user_id)
    if not target or not account or not phone.status(db, target)['can_call']:
        raise HTTPException(409, 'Сначала сохраните настройки браузерного телефона сотрудника')
    if account.active_call:
        raise HTTPException(409, 'Проверка доступна после завершения звонка сотрудника')
    gateway = Gateway()
    try:
        await gateway.open(account.login, phone.cipher().decrypt(account.password_cipher.encode()).decode(), target.full_name)
        async def wait():
            while True:
                for event in await gateway.poll():
                    data = event.get('plugindata', {}).get('data', {})
                    state = data.get('result', {}).get('event')
                    if state == 'registered':
                        return {'state': 'connected', 'checked_at': phone.now().isoformat(), 'calls_started': 0}
                    if state == 'registration_failed' or data.get('error_code'):
                        raise ValueError('Registration rejected')
        return await asyncio.wait_for(wait(), 20)
    except Exception:
        raise HTTPException(502, 'SIP-подключение не подтверждено. Проверьте действующий логин и пароль сотрудника в ВАТС.')
    finally:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(gateway.close(), 5)


class Dial(BaseModel):
    model_config = ConfigDict(extra='forbid')
    phone: str = Field(min_length=10, max_length=40)
    request_id: UUID


@router.post('/entries/{entity_id}/browser-calls')
def prepare(entity_id: int, body: Dial, db: Session = Depends(get_db), user: User = Depends(require_edit(Module.CLIENTS))):
    return phone.prepare(db, user, entity_id, body.phone, body.request_id)


@router.post('/browser-phone/calls/{call_id}/cancel')
def cancel(call_id: UUID, db: Session = Depends(get_db), user: User = Depends(require_edit(Module.CLIENTS))):
    row = db.query(BrowserPhoneCall).filter_by(id=str(call_id), actor_id=user.id).with_for_update().first()
    if not row:
        raise HTTPException(404, 'Звонок не найден')
    if row.state == 'prepared':
        phone.finish(db, row)
    elif row.state not in phone.TERMINAL:
        raise HTTPException(409, 'Завершите звонок в открытой панели телефона')
    return {'state': row.state}


def ws_user(db, token):
    subject = decode_access_token(token) if isinstance(token, str) else None
    try:
        user = db.get(User, int(subject)) if subject is not None else None
    except (TypeError, ValueError):
        return None
    return user if phone.eligible(user) else None


@router.websocket('/browser-phone/ws/{call_id}')
async def voice(ws: WebSocket, call_id: UUID, db: Session = Depends(get_db)):
    origin = ws.headers.get('origin', '')
    if origin and origin not in settings.cors_origins:
        await ws.close(code=4403)
        return
    await ws.accept()
    gateway = None
    row = None
    tasks = []
    try:
        first = await asyncio.wait_for(ws.receive_json(), 10)
        user = ws_user(db, first.get('token')) if isinstance(first, dict) and first.get('type') == 'auth' else None
        candidate = db.get(BrowserPhoneCall, str(call_id))
        if not user or not candidate or candidate.actor_id != user.id or not phone.status(db, user)['can_call']:
            await ws.close(code=4403)
            return
        changed = db.execute(update(BrowserPhoneCall).where(BrowserPhoneCall.id == str(call_id),
                         BrowserPhoneCall.state == 'prepared').values(state='connecting')).rowcount
        db.commit()
        if not changed:
            await ws.send_json({'type': 'error', 'message': 'Этот звонок уже запущен или завершён. Проверьте историю.'})
            return
        row = db.get(BrowserPhoneCall, str(call_id))
        account = db.get(BrowserPhoneAccount, user.id)
        call = db.get(PhoneCall, row.call_id)
        gateway = Gateway()
        await gateway.open(account.login, phone.cipher().decrypt(account.password_cipher.encode()).decode(), user.full_name)
        db.commit()

        async def events():
            while True:
                for event in await gateway.poll():
                    row.updated_at = phone.now()
                    db.commit()
                    if event.get('janus') in ('timeout', 'detached', 'error'):
                        raise ValueError('Voice gateway disconnected')
                    if event.get('janus') == 'trickle':
                        await ws.send_json({'type': 'candidate', 'candidate': event.get('candidate')})
                    data = event.get('plugindata', {}).get('data', {})
                    result = data.get('result', {})
                    state = result.get('event')
                    if data.get('error_code') or state == 'registration_failed':
                        await ws.send_json({'type': 'error', 'message': 'ВАТС отклонила подключение или звонок. Проверьте SIP-логин и пароль сотрудника.'})
                        return
                    if state == 'registered':
                        row.state = 'registered'
                        db.commit()
                        await ws.send_json({'type': 'registered'})
                    elif state in ('calling', 'ringing', 'progress', 'accepted'):
                        if state == 'accepted' and not row.accepted_at:
                            row.accepted_at, row.state = phone.now(), 'active'
                            call.status = 'answered'
                            db.commit()
                            directory = Path(settings.storage_dir) / 'calls' / 'browser'
                            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                            await gateway.message({'request': 'recording', 'action': 'start', 'audio': True,
                                'peer_audio': True, 'filename': str(directory / row.id)})
                        await ws.send_json({'type': state, **({'jsep': event['jsep']} if event.get('jsep') else {})})
                    elif state == 'recordingupdated':
                        row.recording_started = True
                        db.commit()
                    elif state in ('hangup', 'unregistered'):
                        await ws.send_json({'type': 'ended', 'code': result.get('code')})
                        return

        async def commands():
            while True:
                body = await ws.receive_json()
                if not isinstance(body, dict) or len(str(body)) > 100000:
                    raise ValueError('Invalid signal')
                if not ws_user(db, first['token']):
                    raise ValueError('Access expired')
                kind = body.get('type')
                if kind == 'offer' and row.state == 'registered':
                    sdp = body.get('sdp')
                    if not isinstance(sdp, str) or not sdp.startswith('v=0') or '\nm=video' in sdp:
                        raise ValueError('Audio offer required')
                    row.state, call.status = 'calling', 'calling'
                    db.commit()
                    await gateway.message({'request': 'call', 'uri': f'sip:{call.phone}@{settings.browser_phone_domain}',
                                            'call_id': row.id}, {'type': 'offer', 'sdp': sdp, 'trickle': False})
                elif kind == 'candidate':
                    candidate = body.get('candidate')
                    if not isinstance(candidate, dict):
                        raise ValueError('Invalid ICE candidate')
                    await gateway.post('trickle', handle=True, candidate=candidate)
                elif kind == 'hangup':
                    await gateway.message({'request': 'hangup'})
                    return
                elif kind == 'ping':
                    await ws.send_json({'type': 'pong'})

        tasks = [asyncio.create_task(events()), asyncio.create_task(commands())]
        done, pending = await asyncio.wait(tasks, timeout=7200, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    except Exception:
        with contextlib.suppress(Exception):
            await ws.send_json({'type': 'error', 'message': 'Соединение с телефонией прервано. Проверьте историю перед повторным звонком.'})
    finally:
        with CancelScope(shield=True):
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if gateway:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(gateway.close(), 5)
            if row:
                db.rollback()
                phone.finish(db, row, failed=not row.accepted_at)
            with contextlib.suppress(Exception):
                await ws.close()
