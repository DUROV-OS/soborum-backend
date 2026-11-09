from datetime import timedelta
from uuid import uuid4
import pytest
from fastapi import HTTPException
from app.common.module_access import Module, AccessLevel
from app.core.config import settings
from app.core.security import create_access_token
from app.crm.models import CRMEntity
from app.telephony import browser_phone as phone
from app.telephony.models import BrowserPhoneAccount, BrowserPhoneCall, PhoneCall, PhoneRoutingAudit


@pytest.fixture
def voice_setup(db, make_user, monkeypatch):
    monkeypatch.setattr(settings, 'browser_phone_gateway', 'http://voice-gateway:8188/janus')
    monkeypatch.setattr(settings, 'browser_phone_gateway_secret', 'isolated-gateway-secret' * 2)
    user = make_user(Module.CLIENTS)
    director = make_user(admin=True)
    director.job_role = 'director'
    contact = CRMEntity(kind='contact', name='Тестовый покупатель', payload={'phone': '+7 900 000-00-01'})
    db.add(contact); db.commit()
    phone.save_account(db, director, user.id, 'test-sales', 'isolated-sip-secret', True)
    return user, director, contact


def test_account_secret_encrypted_and_director_only(db, api, make_user, voice_setup):
    user, director, contact = voice_setup
    row = db.get(BrowserPhoneAccount, user.id)
    assert 'isolated-sip-secret' not in row.password_cipher
    assert phone.cipher().decrypt(row.password_cipher.encode()) == b'isolated-sip-secret'
    assert 'isolated-sip-secret' not in str(phone.accounts(db))
    assert 'isolated-sip-secret' not in str(db.query(PhoneRoutingAudit).first().details)
    assert api(user).get('/api/crm/browser-phone/accounts').status_code == 403
    other_admin = make_user(admin=True)
    assert api(other_admin).get('/api/crm/browser-phone/accounts').status_code == 403
    response = api(director).get('/api/crm/browser-phone/accounts')
    assert response.status_code == 200 and 'password_cipher' not in response.text
    assert api(director).put(f'/api/crm/browser-phone/accounts/{user.id}', json={
        'login': 'test-sales', 'password': '', 'enabled': True}).status_code == 200
    assert phone.cipher().decrypt(row.password_cipher.encode()) == b'isolated-sip-secret'
    assert api(director).put(f'/api/crm/browser-phone/accounts/{user.id}', json={
        'login': 'new-login', 'password': '', 'enabled': True}).status_code == 422


def test_dial_card_scoping_and_idempotency(db, voice_setup):
    user, director, contact = voice_setup
    request_id = uuid4()
    result = phone.prepare(db, user, contact.id, '89000000001', request_id)
    assert phone.prepare(db, user, contact.id, '+79000000001', request_id) == result
    assert db.query(PhoneCall).count() == db.query(BrowserPhoneCall).count() == 1
    call = db.get(PhoneCall, result['call_id'])
    assert call.crm_entity_id == contact.id and call.processing == 'in_progress'
    with pytest.raises(HTTPException) as error:
        phone.prepare(db, user, contact.id, '+79000000002', uuid4())
    assert error.value.status_code == 422
    with pytest.raises(HTTPException) as error:
        phone.prepare(db, user, contact.id, '+79000000001', uuid4())
    assert error.value.status_code == 409
    contact.archived = True; db.commit()
    with pytest.raises(HTTPException) as error:
        phone.prepare(db, user, contact.id, '+79000000001', uuid4())
    assert error.value.status_code == 409


def test_deal_call_keeps_card_and_owner(db, voice_setup):
    from app.crm.models import CRMEvent
    user, director, contact = voice_setup
    deal = CRMEntity(kind='deal', name='Тестовая заявка', payload={'contact_ids': [contact.id], 'responsible_user_id': director.id})
    db.add(deal); db.commit()
    result = phone.prepare(db, user, deal.id, '+79000000001', uuid4())
    row = db.get(BrowserPhoneCall, result['id'])
    row.accepted_at = phone.now() - timedelta(seconds=25)
    row.recording_started = True
    phone.finish(db, row)
    call = db.get(PhoneCall, row.call_id)
    assert call.deal_id == deal.id and call.crm_entity_id == deal.id
    assert call.status == 'answered' and call.processing == 'local_recording' and call.duration >= 24
    assert deal.payload['responsible_user_id'] == director.id
    events = db.query(CRMEvent).filter_by(entity_id=deal.id).all()
    assert all(event.details['note_type'] == 'call_out' for event in events)
    assert events[-1].details['params']['call_status'] == 'Разговор состоялся'
    assert db.get(BrowserPhoneAccount, user.id).active_call is None
    phone.finish(db, row)
    assert db.query(PhoneCall).count() == 1


def test_viewer_cannot_call_or_change_accounts(db, api, make_user, voice_setup):
    user, director, contact = voice_setup
    viewer = make_user(Module.CLIENTS, level=AccessLevel.VIEW)
    result = api(viewer).post(f'/api/crm/entries/{contact.id}/browser-calls', json={'phone': '+79000000001', 'request_id': str(uuid4())})
    assert result.status_code == 403
    assert api(viewer).get('/api/crm/browser-phone/status').json()['can_call'] is False


def test_cancel_only_own_prepared_call(db, api, make_user, voice_setup):
    user, director, contact = voice_setup
    other = make_user(Module.CLIENTS)
    result = phone.prepare(db, user, contact.id, '+79000000001', uuid4())
    assert api(other).post(f"/api/crm/browser-phone/calls/{result['id']}/cancel").status_code == 404
    assert api(user).post(f"/api/crm/browser-phone/calls/{result['id']}/cancel").json() == {'state': 'ended'}
    assert db.get(PhoneCall, result['call_id']).processing == 'no_recording'


def test_websocket_rejects_impersonation_and_does_not_consume_call(db, api, make_user, voice_setup):
    user, director, contact = voice_setup
    other = make_user(Module.CLIENTS)
    result = phone.prepare(db, user, contact.id, '+79000000001', uuid4())
    client = api(user)
    with client.websocket_connect(f"/api/crm/browser-phone/ws/{result['id']}") as ws:
        ws.send_json({'type': 'auth', 'token': create_access_token(str(other.id))})
        from starlette.websockets import WebSocketDisconnect
        with pytest.raises(WebSocketDisconnect): ws.receive_json()
    assert db.get(BrowserPhoneCall, result['id']).state == 'prepared'


def test_websocket_scoped_call_recording_and_single_use(db, api, voice_setup, monkeypatch):
    import asyncio
    from app.telephony import browser_router
    user, director, contact = voice_setup
    result = phone.prepare(db, user, contact.id, '+79000000001', uuid4())
    commands = []
    class FakeGateway:
        def __init__(self): self.queue = asyncio.Queue(); self.closed = False
        async def open(self, login, password, name):
            assert login == 'test-sales' and password == 'isolated-sip-secret'
            await self.queue.put({'plugindata': {'data': {'result': {'event': 'registered'}}}})
        async def poll(self): return [await self.queue.get()]
        async def post(self, *args, **kwargs): pass
        async def message(self, body, jsep=None):
            commands.append(body)
            if body['request'] == 'call':
                assert body['uri'] == 'sip:79000000001@vats801755.megapbx.ru'
                await self.queue.put({'plugindata': {'data': {'result': {'event': 'accepted'}}}, 'jsep': {'type': 'answer', 'sdp': 'v=0\r\n'}})
            elif body['request'] == 'recording':
                await self.queue.put({'plugindata': {'data': {'result': {'event': 'recordingupdated'}}}})
        async def close(self): self.closed = True
    monkeypatch.setattr(browser_router, 'Gateway', FakeGateway)
    with api(user).websocket_connect(f"/api/crm/browser-phone/ws/{result['id']}") as ws:
        ws.send_json({'type': 'auth', 'token': create_access_token(str(user.id))})
        assert ws.receive_json()['type'] == 'registered'
        ws.send_json({'type': 'offer', 'sdp': 'v=0\r\nm=audio 9 RTP/AVP 0\r\n', 'phone': '79000000099'})
        assert ws.receive_json()['type'] == 'accepted'
        ws.send_json({'type': 'hangup'})
    assert sum(command['request'] == 'call' for command in commands) == 1
    assert any(command.get('audio') and command.get('peer_audio') for command in commands)
    row = db.get(BrowserPhoneCall, result['id'])
    assert row.state == 'ended' and row.accepted_at is not None
    assert db.get(PhoneCall, result['call_id']).processing == 'local_recording'
    with api(user).websocket_connect(f"/api/crm/browser-phone/ws/{result['id']}") as ws:
        ws.send_json({'type': 'auth', 'token': create_access_token(str(user.id))})
        assert ws.receive_json()['type'] == 'error'
    assert sum(command['request'] == 'call' for command in commands) == 1
