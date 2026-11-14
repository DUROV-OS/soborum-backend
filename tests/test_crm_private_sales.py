"""Family/passport boundary, using only synthetic records and temporary files."""
import json
from copy import deepcopy
import pytest
from fastapi import HTTPException
from app.common.module_access import AccessLevel, Module
from app.crm import service
from app.crm.models import CRMEntity, CRMEvent
from app.crm.schemas import Entry, Patch
from app.crm.privacy import PRIVATE_CUSTOMER_FIELDS, private_tokens
from app.ai.live_tools import _read_local_sales_card, _search_local_sales, _read_imported_attachment
from app.moysklad.models import SourceRecord

SECRETS = ['1987-02-19', '2010-04-13', '2017-09-23', 'FAMILY_ONLY_479', 'PASSPORT_ONLY_826', 'unclassified-only-420']


def fixture(db, make_user, tmp_path, monkeypatch):
    from app.core.config import settings
    admin = make_user(admin=True)
    card = service.create(db, 'contact', Entry(name='Публичное имя', data={
        'phone': '+79000000111', 'customer': {'city': 'Тамбов', 'temperature': 'warm',
            'birth_date': SECRETS[0], 'marriage_date': SECRETS[1], 'children_birth_dates': [SECRETS[2]], 'marital_status': SECRETS[3]},
        'custom_fields': [
            {'field_name': 'Паспорт', 'values': [{'value': SECRETS[4]}]},
            {'field_id': 7, 'values': [{'value': SECRETS[5]}]},
            {'field_code': 'PHONE', 'values': [{'value': '+79000000111'}]},
        ]}), admin)
    row = service.get(db, card['id'])
    row.source_id = '991'
    monkeypatch.setattr(settings, 'storage_dir', str(tmp_path))
    root = tmp_path / 'moysklad' / 'files'
    root.mkdir(parents=True)
    (root / 'synthetic-private').write_text(SECRETS[4], encoding='utf-8')
    file = SourceRecord(entity_type='amocrm/files', source_id='private-file', title='Паспорт ' + SECRETS[4],
        search_text=SECRETS[4], archived=False, payload={
            '_linked_entities': [{'id': 991, 'entity_type': 'contacts'}],
            '_downloaded_files': [{'filename': SECRETS[4] + '.txt', 'local_name': 'synthetic-private', 'content_type': 'text/plain'}],
            'url': 'https://example.invalid/' + SECRETS[4]})
    db.add(file)
    db.add(SourceRecord(entity_type='amocrm/contacts/notes', source_id='private-note', title='Imported', archived=False,
        search_text=SECRETS[4], payload={'entity_id': 991, 'created_at': 1793700000, 'params': {'text': 'unclassified ' + SECRETS[4]}}))
    db.add(CRMEvent(entity_id=row.id, actor_id=admin.id, action='note', details={'text': 'Дата рождения ' + SECRETS[0]}))
    db.commit()
    return admin, card, file


def assert_private_absent(result):
    text = json.dumps(result, ensure_ascii=False, default=str)
    assert all(secret not in text for secret in SECRETS)
    assert 'passport_url' not in text and 'passport_file_id' not in text


@pytest.mark.parametrize('level', [AccessLevel.VIEW, AccessLevel.EDIT, AccessLevel.FULL])
def test_all_nonadmin_levels_read_list_search_history_ai_and_files_are_closed(db, make_user, api, tmp_path, monkeypatch, level):
    admin, card, file = fixture(db, make_user, tmp_path, monkeypatch)
    pipeline = service.create(db, 'pipeline', Entry(name='Продажи'), admin)
    stage = service.create(db, 'stage', Entry(name='Новые заявки', data={'pipeline_id': pipeline['id']}), admin)
    service.create(db, 'deal', Entry(name='Публичная сделка', data={
        'pipeline_id': pipeline['id'], 'stage_id': stage['id'], 'amount': 1000,
        'custom_fields': [{'field_name': 'Паспорт', 'values': [{'value': ' '.join(SECRETS)}]}],
    }), admin)
    user = make_user(Module.CLIENTS, Module.AI, level=level)
    client = api(user)
    path = f"/api/crm/entries/{card['id']}"
    for endpoint in (path, '/api/crm/entries?kind=contact', path + '/history', path + '/files', '/api/crm/files'):
        response = client.get(endpoint)
        assert response.status_code == 200
        assert_private_absent(response.json())
    public = client.get(path).json()
    assert public['data']['phone'] == '+79000000111' and public['data']['customer']['city'] == 'Тамбов'
    assert not set(public['data']['customer']) & PRIVATE_CUSTOMER_FIELDS
    assert sum(column['count'] for column in client.get('/api/crm/kanban-summary').json()['columns']) == 1
    for secret in SECRETS:
        assert client.get('/api/crm/entries', params={'kind': 'contact', 'query': secret}).json()['total'] == 0
        summary = client.get('/api/crm/kanban-summary', params={'query': secret}).json()
        assert all(column['count'] == 0 for column in summary['columns'])
        assert sum(column['count'] for column in service.kanban_summary(db, query=secret, user=admin)['columns']) == 1
        assert _search_local_sales(db, user, secret, kind='contact')['total'] == 0
    assert_private_absent(_read_local_sales_card(db, user, card['id']))
    assert client.get(f'/api/crm/files/{file.id}/download').status_code == 403
    assert client.get(f'/api/moysklad/records/{file.id}').status_code == 403
    assert client.get(f'/api/moysklad/records/{file.id}/files/0').status_code == 403
    assert client.get('/api/moysklad/records', params={'entity_type': 'amocrm/files', 'query': SECRETS[4]}).status_code == 403
    with pytest.raises(HTTPException) as denied:
        _read_imported_attachment(db, user, file.id)
    assert denied.value.status_code == 403
    # Bulk API serialization is also the only available CRM export surface;
    # no new spreadsheet/export route is added for the cancelled request #22.
    assert_private_absent(service.list_entries(db, 'contact', limit=100, user=user))


@pytest.mark.parametrize('field,value', [
    ('birth_date', '1980-01-01'), ('marital_status', 'Изменено'), ('marriage_date', None), ('children_birth_dates', []),
])
def test_private_write_rejected_without_value_echo_and_without_mutation(db, make_user, api, tmp_path, monkeypatch, field, value):
    admin, card, file = fixture(db, make_user, tmp_path, monkeypatch)
    user = make_user(Module.CLIENTS, level=AccessLevel.FULL)
    client = api(user)
    before = deepcopy(service.get(db, card['id']).payload)
    count = db.query(CRMEvent).count()
    response = client.patch(f"/api/crm/entries/{card['id']}", json={'version': card['version'], 'data': {'customer': {field: value}}})
    assert response.status_code == 403 and SECRETS[3] not in response.text
    assert service.get(db, card['id']).payload == before and db.query(CRMEvent).count() == count
    created = client.post('/api/crm/entries/contact', json={'name': 'Нельзя', 'data': {'customer': {field: value}}})
    assert created.status_code == 403


def test_public_edits_preserve_hidden_data_and_cannot_clear_private_container(db, make_user, api, tmp_path, monkeypatch):
    admin, card, file = fixture(db, make_user, tmp_path, monkeypatch)
    user = make_user(Module.CLIENTS, level=AccessLevel.FULL)
    client = api(user)
    path = f"/api/crm/entries/{card['id']}"
    visible = client.get(path).json()
    before = deepcopy(service.get(db, card['id']).payload)
    visible['data']['customer']['city'] = 'Воронеж'
    response = client.patch(path, json={'version': visible['version'], 'data': visible['data']})
    assert response.status_code == 200
    assert_private_absent(response.json())
    after = service.get(db, card['id']).payload
    for field in PRIVATE_CUSTOMER_FIELDS:
        assert after['customer'][field] == before['customer'][field]
    assert all(field in after['custom_fields'] for field in before['custom_fields'])
    version = response.json()['version']
    assert client.patch(path, json={'version': version, 'data': {'customer': None}}).status_code == 403
    assert client.patch(path, json={'version': version, 'data': {'custom_fields': [{'field_name': 'Паспорт', 'values': [{'value': 'NEW_SECRET'}]}]}}).status_code == 403
    assert client.patch(path, json={'version': version, 'data': {'inquiry_customer_snapshot': None}}).status_code == 403
    assert client.patch(path, json={'version': version, 'data': {'custom_fields': [
        {'field_id': 7, 'field_name': 'Город', 'values': [{'value': 'Нельзя переименовать закрытое поле'}]},
    ]}}).status_code == 403


def test_public_native_notes_remain_available_and_private_numeric_values_do_not(db, make_user, api, tmp_path, monkeypatch):
    admin, card, file = fixture(db, make_user, tmp_path, monkeypatch)
    user = make_user(Module.CLIENTS, level=AccessLevel.EDIT)
    client = api(user)
    path = f"/api/crm/entries/{card['id']}"
    response = client.post(path + '/notes', json={'text': 'Обсудили планировку, следующий звонок в пятницу'})
    assert response.status_code == 201
    history = client.get(path + '/history').json()
    assert any(item['text'] == 'Обсудили планировку, следующий звонок в пятницу' for item in history['items'])
    assert_private_absent(history)
    assert '1234567890' in private_tokens({'passport_number': 1234567890})


def test_administrator_retains_private_read_write_history_search_and_download(db, make_user, api, tmp_path, monkeypatch):
    admin, card, file = fixture(db, make_user, tmp_path, monkeypatch)
    client = api(admin)
    path = f"/api/crm/entries/{card['id']}"
    assert client.get(path).json()['data']['customer']['birth_date'] == SECRETS[0]
    assert client.get('/api/crm/entries', params={'kind': 'contact', 'query': SECRETS[4]}).json()['total'] == 1
    assert SECRETS[4] in client.get(path + '/history').text
    assert client.get(path + '/files').json()['total'] == 1
    assert client.get(f'/api/crm/files/{file.id}/download').text == SECRETS[4]
    assert _read_imported_attachment(db, admin, file.id)['text'] == SECRETS[4]
    changed = client.patch(path, json={'version': card['version'], 'data': {'customer': {'birth_date': '1988-01-02'}}})
    assert changed.status_code == 200 and changed.json()['data']['customer']['birth_date'] == '1988-01-02'


def test_read_without_actor_is_closed_and_no_imported_family_values_are_promoted(db, make_user, tmp_path, monkeypatch):
    admin, card, file = fixture(db, make_user, tmp_path, monkeypatch)
    row = service.get(db, card['id'])
    before = deepcopy(row.payload)
    assert_private_absent(service.out(row))
    assert_private_absent(service.history(db, row))
    assert service.files(db, entity=row)['items'] == []
    assert row.payload == before
