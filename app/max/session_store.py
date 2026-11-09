"""Private persistent MAX credentials. Tokens never enter API output or logs."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import stat
import time
import uuid

from app.core.config import settings


class StoreUnavailable(RuntimeError):
    pass


def configured():
    return bool(settings.max_session_file)


@contextmanager
def database():
    if not configured():
        raise StoreUnavailable('MAX private storage is not configured')
    path = Path(settings.max_session_file)
    media = Path(settings.storage_dir).resolve()
    if not path.is_absolute() or path.resolve().is_relative_to(media) or path.is_symlink():
        raise StoreUnavailable('MAX private storage location is invalid')
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if os.name == 'posix' and stat.S_IMODE(path.parent.stat().st_mode) & 0o077:
        raise StoreUnavailable('MAX private storage permissions are invalid')
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
    except FileExistsError:
        if not path.is_file() or (os.name == 'posix' and stat.S_IMODE(path.stat().st_mode) & 0o077):
            raise StoreUnavailable('MAX private storage permissions are invalid')
    db = sqlite3.connect(path, timeout=10)
    try:
        db.execute('BEGIN IMMEDIATE')
        db.execute('CREATE TABLE IF NOT EXISTS session (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, at REAL NOT NULL, actor_id INTEGER, action TEXT NOT NULL, account_id TEXT)')
        value = db.execute('SELECT payload FROM session WHERE id=1').fetchone()
        if value is None:
            state = {
                'version': 1, 'generation': 1, 'token': settings.max_token,
                'device_id': str(uuid.uuid4()), 'account_id': None, 'account_name': None,
                'state': 'unchecked' if settings.max_token else 'disconnected',
                'last_connected_at': None, 'last_event_at': None, 'retry_at': None,
                'error_code': None,
            }
            db.execute('INSERT INTO session VALUES (1, ?)', (json.dumps(state),))
        else:
            state = json.loads(value[0])
            if state.get('version') != 1:
                raise StoreUnavailable('MAX private storage version is unsupported')
        yield db, state
        db.execute('UPDATE session SET payload=? WHERE id=1', (json.dumps(state),))
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def read():
    with database() as (_, state):
        return dict(state)


def record(generation, **fields):
    allowed = {'state', 'last_connected_at', 'last_event_at', 'retry_at', 'error_code'}
    if not set(fields).issubset(allowed):
        raise ValueError('Only connection health may be recorded')
    with database() as (_, state):
        if state['generation'] == generation:
            state.update(fields)


def verified(snapshot, token, account_id, account_name):
    with database() as (db, state):
        if state['generation'] != snapshot['generation'] or state['token'] != snapshot['token']:
            return None
        if state['account_id'] and state['account_id'] != account_id:
            raise ValueError('MAX account changed')
        if token != state['token']:
            state['token'] = token
            state['generation'] += 1
            db.execute('INSERT INTO audit(at,action,account_id) VALUES (?,?,?)', (time.time(), 'token_rotated', account_id))
        state.update(account_id=account_id, account_name=account_name, state='connected',
                     error_code=None, retry_at=None, last_connected_at=time.time())
        return state['generation']


def activate(expected_generation, token, device_id, account_id, account_name, actor_id):
    with database() as (db, state):
        if state['generation'] != expected_generation:
            raise ValueError('MAX connection changed while authentication was pending')
        if state['account_id'] and state['account_id'] != account_id:
            raise ValueError('Use the existing corporate MAX account')
        state.update(token=token, device_id=device_id, account_id=account_id,
                     account_name=account_name, generation=state['generation'] + 1,
                     state='connected', error_code=None, retry_at=None,
                     last_connected_at=time.time(), last_event_at=None)
        db.execute('INSERT INTO audit(at,actor_id,action,account_id) VALUES (?,?,?,?)',
                   (time.time(), actor_id, 'owner_confirmed_connection', account_id))
        return state['generation']


def public_status():
    if not configured():
        return {'state': 'not_configured', 'configured': bool(settings.max_token),
                'persistent_session': False, 'automatic_reconnect': False}
    try:
        state = read()
    except Exception:
        return {'state': 'storage_unavailable', 'configured': False,
                'persistent_session': False, 'automatic_reconnect': False}
    result = {key: state.get(key) for key in ('state', 'account_id', 'account_name',
              'last_connected_at', 'last_event_at', 'retry_at', 'error_code')}
    last_seen = max(state.get('last_connected_at') or 0, state.get('last_event_at') or 0)
    if result['state'] == 'connected' and time.time() - last_seen > 300:
        result['state'] = 'reconnecting'
    result.update(configured=bool(state['token']), persistent_session=True,
                  automatic_reconnect=settings.max_live_updates)
    return result
