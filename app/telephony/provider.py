"""MegaFon 'Your CRM' API. Never retry a call-creating request."""
import re
import httpx
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from app.core.config import settings
from app.telephony.models import PhoneDialRequest, PhoneRoutingMember
from app.telephony.routing import eligible, normalize


def configured():
    return bool(re.fullmatch(r'vats[0-9]+\.megapbx\.ru', settings.megafon_domain) and settings.megafon_pbx_token)


def request(command, **data):
    if not configured():
        raise HTTPException(503, 'Ключ управления и адрес ВАТС ещё не настроены')
    # Official contract: https://vats.megafon.ru/elma365_setup.
    with httpx.Client(timeout=httpx.Timeout(12, connect=4), trust_env=False, follow_redirects=False) as client:
        response = client.post(f'https://{settings.megafon_domain}/sys/crm_api.wcgp',
                               json={'cmd': command, 'token': settings.megafon_pbx_token, **data})
    response.raise_for_status()
    return response


def accounts():
    data = request('accounts').json()
    if not isinstance(data, list):
        raise ValueError('Unexpected accounts response')
    return [{'name': str(item.get('name', ''))[:255], 'extension': str(item.get('ext', ''))[:100]}
            for item in data if isinstance(item, dict)]


def dial(db, actor, request_id, number):
    number = normalize(number)
    if not number:
        raise HTTPException(422, 'Укажите один корректный номер телефона')
    key = str(request_id)
    old = db.query(PhoneDialRequest).filter_by(actor_id=actor.id, request_id=key).first()
    if old:
        if old.phone != number:
            raise HTTPException(409, 'Этот запрос уже связан с другим номером')
        return {'request_id': key, 'state': old.state}
    member = db.get(PhoneRoutingMember, actor.id)
    if not member or not member.enabled or not eligible(actor):
        raise HTTPException(409, 'Для сотрудника не назначен логин ВАТС')
    if not configured():
        raise HTTPException(503, 'Звонки через ВАТС пока не подключены')
    row = PhoneDialRequest(actor_id=actor.id, request_id=key, phone=number, pbx_user=member.pbx_user, state='pending')
    try:
        db.add(row)
        # Commit before network I/O: a timeout/crash is never retried on replay.
        db.commit()
    except IntegrityError:
        db.rollback()
        return dial(db, actor, request_id, number)
    try:
        result = request('makeCall', phone=number, user=member.pbx_user)
        if result.content:
            payload = result.json()
            if isinstance(payload, dict) and payload.get('error'):
                row.state = 'rejected'
            else:
                row.state = 'requested'
        else:
            row.state = 'requested'
    except httpx.HTTPStatusError as error:
        # An upstream 5xx can happen after a call was started.
        row.state = 'rejected' if error.response.status_code in (400, 401, 403, 404, 422) else 'unknown'
    except Exception:
        row.state = 'unknown'
    db.commit()
    return {'request_id': key, 'state': row.state}
