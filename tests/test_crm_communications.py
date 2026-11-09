from app.common.module_access import Module, AccessLevel
from app.crm.models import CRMEntity, CRMEvent
from app.crm.chat_models import CRMChatLink
from app.crm import communications
from app.max import service as max_service


def test_explicit_chat_link_inherited_by_deal_and_send_requires_edit(db, api, make_user, monkeypatch):
    contact = CRMEntity(kind='contact', name='Клиент', payload={})
    other = CRMEntity(kind='contact', name='Другой клиент', payload={})
    db.add_all([contact, other]); db.flush()
    deal = CRMEntity(kind='deal', name='Заявка', payload={'contact_ids': [contact.id]})
    db.add(deal); db.commit()
    sends = []
    monkeypatch.setattr(max_service, 'get_chat', lambda id, **kw: {'id': id, 'messages': []})
    monkeypatch.setattr(max_service, 'send_message', lambda id, text: sends.append((id, text)) or {'message': {'id': '123', 'text': text}})
    editor = api(make_user(Module.CLIENTS))
    path = f'/api/crm/entries/{contact.id}/chats'
    assert editor.post(path, json={'chat_id': 123, 'label': 'Клиент MAX'}).status_code == 200
    assert editor.post(path, json={'chat_id': 123, 'label': 'Повтор'}).status_code == 200
    assert db.query(CRMChatLink).count() == 1
    assert db.query(CRMEvent).count() == 1
    inherited = editor.get(f'/api/crm/entries/{deal.id}/chats').json()['items']
    assert len(inherited) == 1 and not inherited[0]['can_unlink']
    assert editor.post(f'/api/crm/entries/{other.id}/chats', json={'chat_id': 123, 'label': 'Чужой'}).status_code == 409
    endpoint = f'/api/crm/entries/{deal.id}/chats/123/messages'
    assert editor.get(endpoint).status_code == 200
    assert editor.post(endpoint, json={'text': '  Расчёт готов  '}).status_code == 201
    assert sends == [(123, 'Расчёт готов')]
    assert editor.post(endpoint.replace('/123/', '/999/'), json={'text': 'Чужой чат'}).status_code == 404
    reader = api(make_user(Module.CLIENTS, level=AccessLevel.VIEW))
    assert reader.get(endpoint).status_code == 200
    assert reader.post(endpoint, json={'text': 'Нельзя'}).status_code == 403
    assert reader.post(path, json={'chat_id': 321, 'label': 'Нельзя'}).status_code == 403
    assert len(sends) == 1


def test_archived_chat_cannot_send_and_unlink_is_scoped(db, api, make_user, monkeypatch):
    owner = make_user(admin=True)
    first = CRMEntity(kind='contact', name='Первый', payload={})
    second = CRMEntity(kind='contact', name='Второй', payload={})
    db.add_all([first, second]); db.commit()
    communications.attach(db, first.id, 456, 'Рабочий чат', owner)
    link = db.query(CRMChatLink).one()
    client = api(owner)
    assert client.delete(f'/api/crm/entries/{second.id}/chats/{link.id}').status_code == 404
    first.archived = True; db.commit()
    assert client.post(f'/api/crm/entries/{first.id}/chats/456/messages', json={'text': 'Нельзя'}).status_code == 409
    first.archived = False; db.commit()
    assert client.delete(f'/api/crm/entries/{first.id}/chats/{link.id}').status_code == 200
    assert db.query(CRMChatLink).count() == 0
    assert db.query(CRMEvent).filter_by(action='chat_unlinked').count() == 1


def test_manager_connection_status_does_not_expose_max_identity(db, make_user, monkeypatch):
    from app.telephony import connections
    monkeypatch.setattr(connections.session_store, 'configured', lambda: True)
    monkeypatch.setattr(connections.session_store, 'public_status', lambda: {
        'configured': True, 'state': 'connected', 'last_connected_at': 1794220000,
        'display_name': 'Private company identity', 'account_id': '12345'})
    result = connections.status(db, make_user(Module.CLIENTS))['max']
    assert set(result) == {'configured', 'state', 'checked_at'}
    assert result['configured'] and result['state'] == 'connected' and result['checked_at']
