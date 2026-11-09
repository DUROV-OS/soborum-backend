"""Card-scoped voice. SIP secrets never leave the server or appear in logs."""
import base64
import hashlib
import hmac
import re
import time
from datetime import datetime, timedelta, timezone
from uuid import UUID
from cryptography.fernet import Fernet
from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from app.core.config import settings
from app.crm import communications
from app.crm.models import CRMEvent
from app.telephony.models import BrowserPhoneAccount, BrowserPhoneCall, PhoneCall, PhoneRoutingAudit
from app.telephony.routing import normalize, eligible
from app.users.models import User

TERMINAL = {'ended', 'failed'}


def now():
    return datetime.now(timezone.utc)


def cipher():
    if settings.browser_phone_encryption_key:
        return Fernet(settings.browser_phone_encryption_key.encode())
    if settings.is_prod:
        raise HTTPException(503, 'Ключ защиты SIP-паролей ещё не настроен')
    key = hmac.new(settings.jwt_secret.encode(), b'durov-os/browser-sip-credentials/v1', hashlib.sha256).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def configured():
    return bool(settings.browser_phone_gateway and len(settings.browser_phone_gateway_secret) >= 32
                and (settings.browser_phone_encryption_key or not settings.is_prod)
                and re.fullmatch(r'vats[0-9]+\.megapbx\.ru', settings.browser_phone_domain))


def status(db, user):
    account = db.get(BrowserPhoneAccount, user.id)
    can_call = bool(configured() and eligible(user) and account and account.enabled and account.password_cipher)
    ice = []
    if can_call and settings.browser_phone_turn_secret and settings.browser_phone_turn_host:
        username = f'{int(time.time()) + 10800}:{user.id}'
        credential = base64.b64encode(hmac.new(settings.browser_phone_turn_secret.encode(), username.encode(), hashlib.sha1).digest()).decode()
        ice = [{'urls': [f'turn:{settings.browser_phone_turn_host}:3478?transport=udp',
                         f'turn:{settings.browser_phone_turn_host}:3478?transport=tcp'],
                'username': username, 'credential': credential}]
    return {'gateway_configured': configured(), 'account_configured': bool(account and account.enabled and account.password_cipher),
            'can_call': can_call, 'ice_servers': ice,
            'login': account.login if account else None, 'domain': settings.browser_phone_domain,
            'mode': 'browser', 'recording': 'server', 'analysis': 'local'}


def accounts(db):
    return [{'user_id': a.user_id, 'login': a.login, 'enabled': a.enabled,
             'password_saved': bool(a.password_cipher)} for a in db.query(BrowserPhoneAccount).order_by(BrowserPhoneAccount.user_id)]


def save_account(db, actor, user_id, login, password, enabled):
    if len(password) > 255:
        raise HTTPException(422, 'SIP-пароль должен содержать не более 255 символов')
    user = db.get(User, user_id)
    if not eligible(user):
        raise HTTPException(422, 'Сотруднику нужен активный доступ к редактированию клиентов')
    if not re.fullmatch(r'[a-zA-Z0-9_.-]{1,100}', login):
        raise HTTPException(422, 'Укажите логин сотрудника ВАТС без домена')
    # Acquire one row lock for concurrent updates/calls on this employee.
    user = db.query(User).filter_by(id=user_id).with_for_update().one()
    row = db.get(BrowserPhoneAccount, user_id)
    if row and row.active_call:
        active = db.get(BrowserPhoneCall, row.active_call)
        if active and active.state not in TERMINAL:
            raise HTTPException(409, 'Дождитесь завершения текущего звонка сотрудника')
    if row and row.login != login and not password:
        raise HTTPException(422, 'Для другого логина введите его действующий SIP-пароль')
    if not row and not password:
        raise HTTPException(422, 'Введите действующий SIP-пароль сотрудника')
    if not row:
        row = BrowserPhoneAccount(user_id=user_id, login=login, password_cipher='')
        db.add(row)
    row.login, row.enabled = login, enabled
    if password:
        row.password_cipher = cipher().encrypt(password.encode()).decode()
    db.add(PhoneRoutingAudit(actor_id=actor.id, details={'action': 'browser_phone_account_updated',
                    'user_id': user_id, 'login': login, 'enabled': enabled}))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, 'Этот SIP-логин уже назначен другому сотруднику')
    return accounts(db)


def prepare(db, user, entity_id, phone, request_id):
    key, phone = str(UUID(str(request_id))), normalize(phone)
    choices = communications.phone_choices(db, entity_id)
    if not phone or phone not in {normalize(p['phone']) for p in choices['items']}:
        raise HTTPException(422, 'Выберите телефон из этой карточки клиента')
    entry = communications.card(db, entity_id, editing=True)
    old = db.get(BrowserPhoneCall, key)
    if old:
        call = db.get(PhoneCall, old.call_id)
        if old.actor_id != user.id or call.phone != phone or entity_id not in (call.crm_entity_id, call.deal_id):
            raise HTTPException(409, 'Запрос уже относится к другому звонку')
        return {'id': old.id, 'call_id': old.call_id, 'state': old.state}
    if not status(db, user)['can_call']:
        raise HTTPException(409, 'Браузерный телефон сотрудника ещё не подключён. Руководитель настраивает его в разделе «Доступ».')
    # Shared database lock prevents two tabs or two application workers calling at once.
    db.execute(update(BrowserPhoneAccount).where(BrowserPhoneAccount.user_id == user.id).values(updated_at=now()))
    account = db.query(BrowserPhoneAccount).filter_by(user_id=user.id).populate_existing().one()
    old = db.get(BrowserPhoneCall, key, populate_existing=True)
    if old:
        call = db.get(PhoneCall, old.call_id)
        if old.actor_id != user.id or call.phone != phone or entity_id not in (call.crm_entity_id, call.deal_id):
            raise HTTPException(409, 'Запрос уже относится к другому звонку')
        return {'id': old.id, 'call_id': old.call_id, 'state': old.state}
    if account.active_call:
        active = db.get(BrowserPhoneCall, account.active_call)
        if active and active.state not in TERMINAL:
            created = active.created_at.replace(tzinfo=timezone.utc) if active.created_at.tzinfo is None else active.created_at
            if created > now() - timedelta(hours=3):
                raise HTTPException(409, 'У вас уже открыт звонок. Завершите его в панели телефона.')
            finish(db, active, failed=True)
    call = PhoneCall(provider='browser_sip', external_id=key, phone=phone, direction='out', employee=user.full_name,
                     started_at=now(), status='preparing', processing='in_progress', match_status='manual',
                     crm_entity_id=entity_id, deal_id=entity_id if entry.kind == 'deal' else None,
                     deal_match_status='manual' if entry.kind == 'deal' else 'unmatched')
    db.add(call)
    db.flush()
    row = BrowserPhoneCall(id=key, actor_id=user.id, call_id=call.id, state='prepared')
    db.add(row)
    account.active_call = key
    db.add(CRMEvent(entity_id=entity_id, actor_id=user.id, action='browser_call_started',
                    details={'text': 'Начат звонок через гарнитуру', 'call_id': call.id, 'phone': phone,
                             'note_type': 'call_out', 'params': {'phone': phone, 'text': 'Начат звонок через гарнитуру'}}))
    db.commit()
    return {'id': key, 'call_id': call.id, 'state': row.state}


def finish(db, row, failed=False):
    if row.state in TERMINAL:
        return
    row.state, row.ended_at = ('failed' if failed else 'ended'), now()
    call = db.get(PhoneCall, row.call_id)
    if row.accepted_at:
        accepted = row.accepted_at.replace(tzinfo=timezone.utc) if row.accepted_at.tzinfo is None else row.accepted_at
        call.duration = max(0, int((row.ended_at - accepted).total_seconds()))
        call.status = 'answered'
        call.processing = 'local_recording'
    else:
        call.status, call.processing = ('failed' if failed else 'not_answered'), 'no_recording'
    account = db.get(BrowserPhoneAccount, row.actor_id)
    if account and account.active_call == row.id:
        account.active_call = None
    db.add(CRMEvent(entity_id=call.deal_id or call.crm_entity_id, actor_id=row.actor_id,
                    action='browser_call_finished', details={'text': 'Звонок завершён', 'call_id': call.id,
                    'status': call.status, 'duration': call.duration, 'note_type': 'call_out',
                    'params': {'phone': call.phone, 'duration': call.duration, 'text': 'Звонок завершён',
                    'call_status': 'Разговор состоялся' if call.status == 'answered' else 'Вызов не подтверждён' if failed else 'Без ответа'}}))
    db.commit()
