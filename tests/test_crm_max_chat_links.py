import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session
from app.common.module_access import AccessLevel, Module
from app.crm import service
from app.crm.chat_models import CRMChatLink
from app.crm.models import CRMEntity, CRMEvent
from app.crm.schemas import Entry
from app.clients import service as clients
from app.clients.schemas import ClientCreate, ClientChatLinkCreate
from app.partners import service as partners
from app.partners.schemas import PartnerCreate, PartnerChatLinkCreate
from app.max import service as max_service


def create_payload(chat=123, kind='contact'):
    return {'max_chat_id': chat, 'create': {'kind': kind, 'name': 'Клиент из чата', 'phone': '+12025550123'}}


@pytest.mark.parametrize('kind', ['contact', 'company'])
def test_create_card_and_link_retry_is_idempotent(api, make_user, db, kind):
    user = make_user(Module.CLIENTS)
    client = api(user)
    response = client.post('/api/crm/chat-links/from-max', json=create_payload(kind=kind))
    assert response.status_code == 201, response.text
    link = response.json()
    row = db.get(CRMEntity, link['entity_id'])
    assert row.kind == kind and row.payload['phone'] == '+12025550123'
    assert row.payload['responsible_user_id'] == user.id
    retry = client.post('/api/crm/chat-links/from-max', json=create_payload(kind=kind))
    assert retry.status_code == 201 and retry.json()['id'] == link['id']
    assert db.query(CRMEntity).count() == 1 and db.query(CRMChatLink).count() == 1
    assert db.query(CRMEvent).count() == 1
    assert client.get(f"/api/crm/entries/{row.id}/chat-links").json() == [link]


def test_existing_card_link_is_stable_and_other_target_rejected(api, make_user, db):
    user = make_user(Module.CLIENTS)
    first = service.create(db, 'contact', Entry(name='Первый'), user)
    other = service.create(db, 'company', Entry(name='Второй'), user)
    client = api(user)
    original = client.post('/api/crm/chat-links/from-max', json={'max_chat_id': -123, 'entity_id': first['id']})
    assert original.status_code == 201
    assert client.post('/api/crm/chat-links/from-max', json={'max_chat_id': -123, 'entity_id': first['id']}).json() == original.json()
    assert client.post('/api/crm/chat-links/from-max', json={'max_chat_id': -123, 'entity_id': other['id']}).status_code == 409
    assert client.post('/api/crm/chat-links/from-max', json=create_payload(-123, 'company')).status_code == 409
    assert db.query(CRMEntity).count() == 2


@pytest.mark.parametrize('legacy_kind', ['client', 'partner'])
def test_all_binding_writers_reject_cross_table_conflicts(api, make_user, db, legacy_kind):
    user = make_user(Module.CLIENTS)
    client = api(user)
    if legacy_kind == 'client':
        legacy = clients.create_client(db, ClientCreate(full_name='ERP', phone='+12025550124', email='erp@example.test'))
        bind = lambda chat: clients.create_chat_link(db, legacy, ClientChatLinkCreate(max_chat_id=chat, label='MAX'))
    else:
        legacy = partners.create_partner(db, PartnerCreate(category='REALTOR', name='Партнёр', city='Москва'), user.id)
        bind = lambda chat: partners.create_chat_link(db, legacy, PartnerChatLinkCreate(max_chat_id=chat, label='MAX'))
    bind(125); db.commit()
    assert client.post('/api/crm/chat-links/from-max', json=create_payload(125)).status_code == 409
    assert db.query(CRMEntity).count() == 0
    assert client.post('/api/crm/chat-links/from-max', json=create_payload(126)).status_code == 201
    with pytest.raises(HTTPException) as conflict:
        bind(126)
    assert conflict.value.status_code == 409
    db.rollback()


def test_permissions_and_annotation_do_not_leak_crm_names(api, make_user, db, monkeypatch):
    owner = make_user(Module.CLIENTS)
    editor = api(owner)
    created = editor.post('/api/crm/chat-links/from-max', json=create_payload()).json()
    monkeypatch.setattr(max_service, 'list_chats', lambda limit=None: {'chats': [{'id': 123}], 'count': 1})
    viewer = api(make_user(Module.CLIENTS, level=AccessLevel.VIEW))
    assert viewer.get(f"/api/crm/entries/{created['entity_id']}/chat-links").status_code == 200
    assert viewer.post('/api/crm/chat-links/from-max', json=create_payload(124)).status_code == 403
    assert viewer.get('/api/max/chats').json()['chats'][0]['linkedCrmId'] == created['entity_id']
    outsider = api(make_user(Module.WAREHOUSE))
    assert outsider.get(f"/api/crm/entries/{created['entity_id']}/chat-links").status_code == 403
    assert outsider.post('/api/crm/chat-links/from-max', json=create_payload(124)).status_code == 403
    assert outsider.get('/api/max/chats').json()['chats'][0]['linkedCrmId'] is None
    assert outsider.get('/api/max/chats').json()['chats'][0]['linkedCrmName'] is None


def test_link_failure_rolls_back_created_card_and_history(api, make_user, db, monkeypatch):
    user = make_user(Module.CLIENTS)
    original = Session.flush
    def fail_link(self, *args, **kwargs):
        if any(isinstance(item, CRMChatLink) for item in self.new):
            raise HTTPException(503, 'Synthetic link failure')
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Session, 'flush', fail_link)
    result = api(user).post('/api/crm/chat-links/from-max', json=create_payload())
    assert result.status_code == 503
    assert db.query(CRMEntity).count() == db.query(CRMEvent).count() == db.query(CRMChatLink).count() == 0


@pytest.mark.parametrize('patch', [
    {'max_chat_id': 0}, {'max_chat_id': 9007199254740992}, {'max_chat_id': -9007199254740992},
    {'label': '   '}, {'entity_id': 1}, {'create': {'kind': 'deal', 'name': 'Invalid'}},
])
def test_invalid_inputs_do_not_create_records(api, make_user, db, patch):
    response = api(make_user(Module.CLIENTS)).post('/api/crm/chat-links/from-max', json={**create_payload(), **patch})
    assert response.status_code == 422
    assert db.query(CRMEntity).count() == db.query(CRMChatLink).count() == 0


def test_archived_existing_card_is_rejected(api, make_user, db):
    user = make_user(Module.CLIENTS)
    archived = service.create(db, 'contact', Entry(name='Архив', archived=True), user)
    response = api(user).post('/api/crm/chat-links/from-max', json={'max_chat_id': 127, 'entity_id': archived['id']})
    assert response.status_code == 422
    assert db.query(CRMChatLink).count() == 0
