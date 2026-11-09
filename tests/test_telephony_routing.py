from contextlib import contextmanager
from uuid import uuid4
import httpx
import pytest
from fastapi import HTTPException
from app.common.module_access import Module, AccessLevel
from app.core.config import settings
from app.crm.models import CRMEntity
from app.telephony import routing, service, provider, connections
from app.telephony.models import PhoneRoutingConfig, PhoneRoutingMemory, PhoneRoutingMember, PhoneCall, PhoneDialRequest


@pytest.fixture
def pool(db, make_user):
    first, second = make_user(Module.CLIENTS), make_user(Module.CLIENTS)
    actor = make_user(admin=True)
    config = routing.Configuration(version=1, enabled=True, pbx_setup_confirmed=True,
        members=[routing.Member(user_id=first.id, pbx_user='101'), routing.Member(user_id=second.id, pbx_user='102')])
    routing.save_config(db, config, actor)
    return first, second, actor


def incoming(db, number, **extra):
    return service.ingest(db, {'cmd': 'contact', 'phone': number, **extra})


def test_equal_new_assignments_repeat_normalization_and_restart(db, pool):
    for index in range(20):
        result = incoming(db, '7900111' + str(index).zfill(4))
        assert result['responsible'] == ('101' if index % 2 == 0 else '102')
    for value in ('+7 (900) 111-00-00', '89001110000', '9001110000'):
        assert incoming(db, value)['responsible'] == '101'
    db.expire_all()  # round-robin and ownership live in the DB, not process memory
    assert incoming(db, '79001110020')['responsible'] == '101'
    assert db.get(PhoneRoutingConfig, 1).cursor == 21
    assert db.query(PhoneRoutingMemory).count() == 21
    assert db.query(PhoneCall).count() == 0  # contact lookup is not a real call


def test_overflow_answer_preserves_assigned_owner_and_crm_ownership_wins(db, pool):
    first, second, actor = pool
    assert incoming(db, '79001112233')['responsible'] == '101'
    service.ingest(db, {'cmd': 'event', 'callid': 'overflow', 'phone': '79001112233', 'type': 'ACCEPTED', 'ext': '102'})
    assert db.get(PhoneRoutingMemory, '79001112233').user_id == first.id
    assert db.get(PhoneRoutingMemory, '79001112233').confirmed
    assert db.query(PhoneCall).one().direction == 'in'
    assert incoming(db, '79001112233')['responsible'] == '101'
    card = CRMEntity(kind='contact', name='Existing customer', payload={'phone': '79001112233', 'responsible_user_id': second.id})
    db.add(card); db.commit()
    assert incoming(db, '79001112233')['responsible'] == '102'
    assert card.payload['responsible_user_id'] == second.id


def test_secondary_phone_and_deal_owner_are_used_without_mutating_crm(db, pool):
    first, second, actor = pool
    contact = CRMEntity(kind='contact', name='Customer', payload={'customer': {'phone_2': '89001112233'}})
    db.add(contact); db.flush()
    deal = CRMEntity(kind='deal', name='House', payload={'contact_ids': [contact.id], 'responsible_user_id': second.id})
    db.add(deal); db.commit()
    before = dict(deal.payload)
    assert incoming(db, '79001112233')['responsible'] == '102'
    assert deal.payload == before and db.get(PhoneRoutingConfig, 1).cursor == 0


def test_ambiguous_owners_use_pbx_queue_and_do_not_assign_memory(db, pool):
    first, second, actor = pool
    for user in (first, second):
        db.add(CRMEntity(kind='contact', name='Shared number', payload={'phone': '79001112233', 'responsible_user_id': user.id}))
    db.commit()
    assert incoming(db, '79001112233') == {'contact_name': ''}
    assert db.query(PhoneRoutingMemory).count() == 0


def test_absent_manager_uses_queue_without_stealing_client(db, pool):
    first, second, actor = pool
    incoming(db, '79001112233')
    first.is_active = False; db.commit()
    assert 'responsible' not in incoming(db, '79001112233')
    assert db.get(PhoneRoutingMemory, '79001112233').user_id == first.id
    assert incoming(db, '79001112234')['responsible'] == '102'
    first.is_active = True; db.commit()
    assert incoming(db, '79001112233')['responsible'] == '101'


def test_disabled_or_other_company_number_never_advances_pool(db, pool):
    assert 'responsible' not in incoming(db, '79001112233', diversion='79002223344')
    cfg = db.get(PhoneRoutingConfig, 1); cfg.enabled = False; db.commit()
    assert 'responsible' not in incoming(db, '79001112233')
    assert cfg.cursor == 0 and db.query(PhoneRoutingMemory).count() == 0


def test_live_event_without_call_id_remembers_manager_without_inventing_call(db, pool):
    for _ in range(2):
        assert service.ingest(db, {'cmd': 'event', 'type': 'ACCEPTED', 'phone': '79001112233', 'ext': '102'}) == {'result': 'ok'}
    assert db.query(PhoneCall).count() == 0
    assert db.query(PhoneRoutingMemory).count() == 1
    assert incoming(db, '79001112233')['responsible'] == '102'
    assert db.get(PhoneRoutingConfig, 1).cursor == 0


def test_unmapped_imported_owner_falls_back_without_invalid_user_fk(db, pool):
    db.add(CRMEntity(kind='contact', name='Imported', payload={'phone': '79001112233', 'responsible_user_id': 999999999}))
    db.commit()
    assert 'responsible' not in incoming(db, '79001112233')
    assert db.query(PhoneRoutingMemory).count() == 0


def test_config_permissions_version_and_draft_does_not_activate(db, pool, api, make_user, monkeypatch):
    first, second, actor = pool
    for user in (first, make_user(Module.CLIENTS, level=AccessLevel.VIEW)):
        assert api(user).get('/api/crm/telephony/routing').status_code == 403
    client = api(actor)
    value = client.get('/api/crm/telephony/routing').json()
    value.pop('fallback')
    value['enabled'] = False
    assert client.put('/api/crm/telephony/routing', json=value).status_code == 200
    assert client.put('/api/crm/telephony/routing', json=value).status_code == 409
    value['version'] += 1; value['enabled'] = True
    monkeypatch.setattr(settings, 'megafon_crm_token', '')
    assert client.put('/api/crm/telephony/routing', json=value).status_code == 422
    assert not db.get(PhoneRoutingConfig, 1).enabled


def test_webhook_contact_has_no_call_id_and_rejects_bad_secret_or_duplicates(db, pool, api, monkeypatch):
    monkeypatch.setattr(settings, 'megafon_crm_token', 'c' * 32)
    client = api(pool[2])
    path = '/api/telephony/megafon'
    body = {'cmd': 'contact', 'phone': '79001112233', 'crm_token': 'c' * 32}
    assert client.post(path, data=body).json()['responsible'] == '101'
    body['crm_token'] = 'wrong'
    assert client.post(path, data=body).status_code == 401
    assert client.post(path, content='cmd=contact&cmd=event').status_code == 400
    assert db.query(PhoneRoutingMemory).count() == 1


@pytest.mark.parametrize('number', ['letters79001112233', '12345', '79001112233,79002223344', ''])
def test_invalid_phone_cannot_consume_an_assignment(db, pool, number):
    with pytest.raises(HTTPException):
        incoming(db, number)
    assert db.get(PhoneRoutingConfig, 1).cursor == 0


def test_dial_is_explicit_single_shot_and_does_not_expose_credentials(db, pool, api, monkeypatch):
    first, second, actor = pool
    monkeypatch.setattr(settings, 'megafon_domain', 'vats801755.megapbx.ru')
    monkeypatch.setattr(settings, 'megafon_pbx_token', 'private-api-key')
    requests = []
    def request(command, **data):
        requests.append((command, data))
        return httpx.Response(200, json={'result': 'ok'})
    monkeypatch.setattr(provider, 'request', request)
    client = api(first)
    assert client.get('/api/crm/communications/status').json()['megafon']['can_call']
    assert requests == []
    body = {'phone': '+79001112233', 'request_id': str(uuid4())}
    one = client.post('/api/crm/calls/dial', json=body)
    two = client.post('/api/crm/calls/dial', json=body)
    assert one.json() == two.json() and one.json()['state'] == 'requested'
    assert requests == [('makeCall', {'phone': '79001112233', 'user': '101'})]
    assert 'private-api-key' not in one.text
    assert client.post('/api/crm/calls/dial', json={**body, 'phone': '79001112234'}).status_code == 409


def test_timeout_dial_is_unknown_and_never_retried(db, pool, monkeypatch):
    first = pool[0]
    monkeypatch.setattr(provider, 'configured', lambda: True)
    requests = []
    def request(*args, **kwargs):
        requests.append(1)
        raise httpx.ReadTimeout('Do not expose secret URL')
    monkeypatch.setattr(provider, 'request', request)
    key = uuid4()
    assert provider.dial(db, first, key, '79001112233')['state'] == 'unknown'
    assert provider.dial(db, first, key, '79001112233')['state'] == 'unknown'
    assert len(requests) == 1 and db.query(PhoneDialRequest).count() == 1


def test_read_only_manager_cannot_dial_even_with_old_pbx_mapping(db, make_user, api, monkeypatch):
    reader = make_user(Module.CLIENTS, level=AccessLevel.VIEW)
    db.add(PhoneRoutingMember(user_id=reader.id, pbx_user='103', enabled=True)); db.commit()
    monkeypatch.setattr(provider, 'configured', lambda: True)
    client = api(reader)
    assert not client.get('/api/crm/communications/status').json()['megafon']['can_call']
    assert client.post('/api/crm/calls/dial', json={'request_id': str(uuid4()), 'phone': '79001112233'}).status_code == 403


def test_check_reports_max_reauthentication_without_returning_credentials(db, pool, api, monkeypatch):
    monkeypatch.setattr(settings, 'max_token', 'private-max-token')
    monkeypatch.setattr(provider, 'accounts', lambda: [{'name': 'Sales', 'extension': '101'}])
    @contextmanager
    def rejected():
        raise HTTPException(502, 'Рабочий аккаунт MAX требует повторного входа владельцем')
        yield
    monkeypatch.setattr(connections, 'session', rejected)
    with connections._lock:
        connections._last.clear()
    result = api(pool[2]).post('/api/crm/communications/check')
    assert result.json()['max']['state'] == 'reauth_required'
    assert 'private-max-token' not in result.text
    assert api(pool[0]).post('/api/crm/communications/check').status_code == 403
    with connections._lock:
        connections._last.clear()
