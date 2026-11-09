"""Recover connections and token rotation; never replay message sends."""
from fastapi import HTTPException
from app.max import session_store as store


def profile_name(session):
    from app.max.service import _contact_name
    profile = session.login.get('profile') or {}
    return str(_contact_name(profile.get('contact') or profile) or 'Рабочий аккаунт MAX')[:160]


def open_session():
    from app.max.client import AuthRejected, MaxSession
    from app.core.config import settings
    if not store.configured():
        if not settings.max_token:
            raise HTTPException(503, 'Рабочий аккаунт MAX ещё не подключён')
        return MaxSession(settings.max_token).open()
    for attempt in range(2):
        snapshot = store.read()
        if snapshot['state'] in ('reauth_required', 'account_mismatch'):
            raise AuthRejected()
        if not snapshot['token']:
            raise HTTPException(503, 'Владельцу нужно подключить рабочий аккаунт MAX по QR-коду')
        session = MaxSession(snapshot['token'], device_id=snapshot['device_id'])
        try:
            session.open()
        except AuthRejected:
            current = store.read()
            if attempt == 0 and current['generation'] != snapshot['generation']:
                continue
            store.record(snapshot['generation'], state='reauth_required', error_code='auth_rejected', retry_at=None)
            raise
        except Exception:
            store.record(snapshot['generation'], state='reconnecting', error_code='connection_failed')
            raise
        if snapshot['account_id'] and session.viewer_id() != snapshot['account_id']:
            session.close()
            store.record(snapshot['generation'], state='account_mismatch', error_code='account_mismatch', retry_at=None)
            raise AuthRejected()
        try:
            generation = store.verified(snapshot, session.token, session.viewer_id(), profile_name(session))
        except Exception:
            session.close()
            raise
        if generation is not None:
            session.managed_generation = generation
            return session
        session.close()
    raise HTTPException(503, 'Сессия MAX обновилась. Повторите действие.')
