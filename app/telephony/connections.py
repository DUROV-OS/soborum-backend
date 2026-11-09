"""Read-only diagnostics. Configuration alone never means connected."""
from datetime import datetime, timezone
from threading import Lock
from fastapi import HTTPException
from app.core.config import settings
from app.max.client import session
from app.telephony import provider
from app.telephony.models import PhoneRoutingMember
from app.telephony.routing import eligible

_lock = Lock()
_last = {}


def status(db, user):
    member = db.get(PhoneRoutingMember, user.id)
    with _lock:
        checked = dict(_last)
    if checked.get('checked_at') and (datetime.now(timezone.utc) - datetime.fromisoformat(checked['checked_at'])).total_seconds() > 300:
        checked = {}
    return {
        'megafon': {
            'configured': provider.configured(),
            'webhook_configured': len(settings.megafon_crm_token) >= 32,
            'state': checked.get('megafon', {}).get('state', 'unchecked') if provider.configured() else 'not_configured',
            'checked_at': checked.get('checked_at'),
            'can_call': bool(provider.configured() and member and member.enabled and eligible(user)),
            'extension': (user.telephony_extension or member.pbx_user) if member else None,
        },
        'max': {
            'configured': bool(settings.max_token),
            'state': checked.get('max', {}).get('state', 'unchecked') if settings.max_token else 'not_configured',
            'checked_at': checked.get('checked_at'),
        },
    }


def check():
    result = {'checked_at': datetime.now(timezone.utc).isoformat()}
    try:
        members = provider.accounts()
        result['megafon'] = {'state': 'connected', 'accounts': members}
    except Exception:
        result['megafon'] = {'state': 'unavailable' if provider.configured() else 'not_configured'}
    if not settings.max_token:
        result['max'] = {'state': 'not_configured'}
    else:
        try:
            with session() as client:
                result['max'] = {'state': 'connected' if client.viewer_id() else 'reauth_required'}
        except HTTPException as error:
            result['max'] = {'state': 'reauth_required' if 'повторного входа' in str(error.detail) else 'unavailable'}
        except Exception:
            result['max'] = {'state': 'unavailable'}
    with _lock:
        _last.update(result)
    return result
