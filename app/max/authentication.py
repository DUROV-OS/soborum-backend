"""Owner-driven QR renewal. Candidates stay private until account confirmation.

Wire fields checked against MaxApiTeam/PyMax (MIT), api/auth and auth/qr.
No SMS requests, password changes, message sends or automatic QR approvals.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
from io import BytesIO
import threading
import time
from urllib.parse import urlsplit
import uuid

from fastapi import HTTPException
import qrcode

from app.max.client import MaxSession
from app.max.connection import profile_name
from app.max import session_store as store


@dataclass(repr=False)
class Attempt:
    id: str
    actor_id: int
    generation: int
    device_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    phase: str = 'starting'
    expires_at: float = field(default_factory=lambda: time.time() + 600)
    qr_image: str | None = None
    account_id: str | None = None
    account_name: str | None = None
    error: str | None = None
    token: str = ''
    password: str = ''
    socket: MaxSession | None = None
    stop: threading.Event = field(default_factory=threading.Event)
    password_ready: threading.Event = field(default_factory=threading.Event)


_lock = threading.RLock()
_attempt: Attempt | None = None
TERMINAL = {'complete', 'cancelled', 'expired', 'failed', 'account_mismatch'}


def _discard(job, phase):
    job.phase, job.token, job.password, job.qr_image = phase, '', '', None
    job.stop.set()
    job.password_ready.set()
    if job.socket:
        job.socket.close()


def _expire(job):
    with _lock:
        if job.phase not in TERMINAL:
            _discard(job, 'expired')


def _public(job):
    if time.time() >= job.expires_at and job.phase not in TERMINAL:
        _discard(job, 'expired')
    return {key: getattr(job, key) for key in ('id', 'phase', 'expires_at', 'qr_image',
             'account_id', 'account_name', 'error')}


def _owned(attempt_id, actor_id):
    if _attempt is None or _attempt.id != attempt_id or _attempt.actor_id != actor_id:
        raise HTTPException(404, 'Попытка подключения не найдена. Получите новый QR-код.')
    return _attempt


def get(attempt_id, actor_id):
    with _lock:
        return _public(_owned(attempt_id, actor_id))


def start(request_id, actor_id):
    global _attempt
    if not store.configured():
        raise HTTPException(503, 'Приватное хранилище сессии MAX ещё не настроено на сервере')
    try:
        state = store.read()
    except Exception:
        raise HTTPException(503, 'Приватное хранилище сессии MAX недоступно') from None
    with _lock:
        if _attempt and _attempt.id == str(request_id) and _attempt.actor_id == actor_id:
            return _public(_attempt)
        if _attempt and _attempt.phase not in TERMINAL:
            _public(_attempt)
        if _attempt and _attempt.phase not in TERMINAL:
            if _attempt.phase == 'connecting':
                raise HTTPException(409, 'Подключение уже сохраняется. Дождитесь результата.')
            if _attempt.actor_id != actor_id:
                raise HTTPException(409, 'Другой руководитель уже подключает MAX')
            _discard(_attempt, 'cancelled')
        job = _attempt = Attempt(str(request_id), actor_id, state['generation'])
        timer = threading.Timer(600, _expire, args=(job,))
        timer.daemon = True
        timer.start()
        threading.Thread(target=_run, args=(job,), name='max-qr', daemon=True).start()
        return _public(job)


def cancel(attempt_id, actor_id):
    with _lock:
        job = _owned(attempt_id, actor_id)
        if job.phase == 'connecting':
            raise HTTPException(409, 'Подключение уже сохраняется. Дождитесь результата.')
        if job.phase != 'complete':
            _discard(job, 'cancelled')
        return _public(job)


def password(attempt_id, actor_id, value):
    with _lock:
        job = _owned(attempt_id, actor_id)
        _public(job)
        if job.phase != 'password_required':
            raise HTTPException(409, 'Пароль для этой попытки не запрашивался')
        if not isinstance(value, str) or not 1 <= len(value) <= 1024:
            raise HTTPException(422, 'Введите пароль входа MAX')
        job.password, job.phase, job.error = value, 'checking_password', None
        job.password_ready.set()
        return _public(job)


def _publish(job, **fields):
    with _lock:
        if time.time() >= job.expires_at:
            _discard(job, 'expired')
        if job.stop.is_set() or _attempt is not job:
            return False
        for key, value in fields.items():
            setattr(job, key, value)
        return True


def _request(session, opcode, payload):
    seq = session._send(opcode, payload)
    reply = session._wait(opcode, seq)
    body = reply.get('payload')
    if reply.get('cmd') != 1 or not isinstance(body, dict) or body.get('error'):
        raise RuntimeError('MAX authentication step rejected')
    return body


def _login_token(payload):
    value = ((payload.get('tokenAttrs') or {}).get('LOGIN') or {}).get('token')
    if isinstance(value, str) and 1 <= len(value) <= 16384:
        return value
    return ''


def _qr_image(link):
    parsed = urlsplit(link)
    host = parsed.hostname or ''
    if len(link) > 4096 or parsed.scheme != 'https' or parsed.username or parsed.password or not (
        host == 'max.ru' or host.endswith('.max.ru') or host == 'oneme.ru' or host.endswith('.oneme.ru')
    ):
        raise RuntimeError('Invalid MAX QR link')
    image = qrcode.make(link)
    stream = BytesIO()
    image.save(stream, format='PNG')
    return 'data:image/png;base64,' + base64.b64encode(stream.getvalue()).decode('ascii')


def _run(job):
    session = MaxSession('', device_id=job.device_id)
    job.socket = session
    try:
        if job.stop.is_set():
            return
        session.open_transport()
        qr = _request(session, 288, {})
        expires = min(float(qr['expiresAt']) / 1000, time.time() + 600)
        interval = max(1, min(float(qr['pollingInterval']) / 1000, 15))
        track = str(qr['trackId'])
        if expires <= time.time() or not track:
            raise RuntimeError('Expired MAX QR')
        if not _publish(job, phase='waiting', expires_at=expires, qr_image=_qr_image(qr['qrLink'])):
            return
        confirmed = False
        while time.time() < expires and not job.stop.is_set():
            state = _request(session, 289, {'trackId': track})
            if (state.get('status') or {}).get('loginAvailable') is True:
                confirmed = True
                break
            job.stop.wait(interval)
        if not confirmed:
            _expire(job)
            return
        reply = _request(session, 291, {'trackId': track})
        token = _login_token(reply)
        challenge = reply.get('passwordChallenge') or {}
        if not token and challenge.get('trackId'):
            for _ in range(3):
                job.password_ready.clear()
                if not _publish(job, phase='password_required', qr_image=None,
                                expires_at=min(time.time() + 300, expires + 300)):
                    return
                job.password_ready.wait(max(0, job.expires_at - time.time()))
                with _lock:
                    if job.stop.is_set() or not job.password:
                        return _expire(job)
                    value, job.password = job.password, ''
                try:
                    reply = _request(session, 115, {'trackId': challenge['trackId'], 'password': value})
                    token = _login_token(reply)
                except Exception:
                    _publish(job, error='MAX не принял пароль. Проверьте его и введите ещё раз.')
                finally:
                    value = ''
                if token:
                    break
        if not token:
            raise RuntimeError('MAX did not issue login token')
        session.close()
        session = MaxSession(token, device_id=job.device_id)
        job.socket = session
        session.open()
        current = store.read()
        if current['account_id'] and current['account_id'] != session.viewer_id():
            with _lock:
                _discard(job, 'account_mismatch')
            return
        _publish(job, phase='confirm_account', token=session.token, qr_image=None,
                 account_id=session.viewer_id(), account_name=profile_name(session),
                 error=None, expires_at=time.time() + 300)
    except Exception:
        with _lock:
            if not job.stop.is_set():
                job.error = 'MAX не завершил вход. Получите новый QR-код и повторите подтверждение.'
                _discard(job, 'failed')
    finally:
        session.close()
        job.socket = None


def confirm(attempt_id, actor_id):
    with _lock:
        job = _owned(attempt_id, actor_id)
        _public(job)
        if job.phase == 'complete':
            return _public(job)
        if job.phase != 'confirm_account' or not job.token:
            raise HTTPException(409, 'Сначала подтвердите вход в MAX')
        job.phase = 'connecting'
        token = job.token
    session = MaxSession(token, device_id=job.device_id)
    try:
        session.open()
        if session.viewer_id() != job.account_id:
            raise ValueError('MAX account changed')
        # Serialize the final commit with expiry/cancellation/new attempts.
        with _lock:
            if _attempt is not job or job.stop.is_set() or time.time() >= job.expires_at:
                raise ValueError('MAX authentication expired before commit')
            store.activate(job.generation, session.token, job.device_id, session.viewer_id(), profile_name(session), actor_id)
            _discard(job, 'complete')
    except ValueError:
        with _lock:
            job.error = 'Сессия или аккаунт изменились. Обновите состояние и подключитесь заново.'
            _discard(job, 'failed')
        raise HTTPException(409, job.error) from None
    except Exception:
        with _lock:
            job.error = 'Не удалось сохранить подтверждённое подключение MAX. Повторите вход.'
            _discard(job, 'failed')
        raise HTTPException(502, job.error) from None
    finally:
        session.close()
    from app.max.listener import start_max_listener, wake
    start_max_listener()
    wake()
    return get(attempt_id, actor_id)
