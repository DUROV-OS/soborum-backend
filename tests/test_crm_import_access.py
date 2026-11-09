"""Synthetic-only classification lifecycle and every imported content route."""
from copy import deepcopy
from hashlib import sha256
import json
import pytest
from fastapi import HTTPException

from app.ai.live_tools import _read_imported_attachment, _read_local_sales_card
from app.ai.tools import _read_attached_file
from app.common.files import FileAsset, FilePurpose
from app.common.module_access import AccessLevel, Module
from app.crm import service
from app.crm.import_access import ReviewInput, fingerprint, record_review
from app.crm.models import CRMImportReview
from app.crm.schemas import Entry
from app.moysklad.models import SourceRecord


def setup(db, make_user, monkeypatch, tmp_path):
    from app.core.config import settings
    monkeypatch.setattr(settings, 'storage_dir', str(tmp_path))
    root = tmp_path / 'moysklad' / 'files'
    root.mkdir(parents=True)
    content = b'Business-only synthetic specification, no personal data.'
    path = root / 'synthetic-document'
    path.write_bytes(content)
    admin = make_user(admin=True)
    card = service.create(db, 'contact', Entry(name='Business contact'), admin)
    service.get(db, card['id']).source_id = '12345'
    note = SourceRecord(entity_type='amocrm/contacts/notes', source_id='note', title='Unknown old note', archived=False,
        search_text='private source text', payload={'entity_id': 12345, 'created_at': 1793700000,
            'note_type': 'common', 'params': {'text': 'Private original PASSPORT_MARKER', 'unknown': 'HIDDEN_VALUE'}})
    call = SourceRecord(entity_type='amocrm/contacts/notes', source_id='call', title='Call', archived=False,
        search_text='private result', payload={'entity_id': 12345, 'created_at': 1793700001, 'note_type': 'call_in',
            'params': {'phone': '+79001112233', 'duration': 54, 'call_result': 'PASSPORT_MARKER',
                'link': 'https://example.invalid/PRIVATE_AUDIO', 'extra': {'birth_date': '1990-01-01'}}})
    file = SourceRecord(entity_type='amocrm/files', source_id='file', title='PRIVATE_FILENAME', archived=False,
        search_text='PRIVATE_SEARCH', payload={'_linked_entities': [{'id': 12345, 'entity_type': 'contacts'}],
            '_downloaded_files': [{'local_name': path.name, 'filename': 'PRIVATE_FILENAME.txt', 'content_type': 'text/plain'}],
            'url': 'https://example.invalid/PRIVATE_URL'})
    db.add_all([note, call, file])
    db.flush()
    alias = FileAsset(filename='PRIVATE_ALIAS.txt', content_type='text/plain', path_on_disk=str(path),
        purpose=FilePurpose.CONTRACT, uploaded_by_id=admin.id)
    db.add(alias)
    db.commit()
    return admin, card, note, call, file, alias, path, content


def classify(db, admin, row, *, index=-1, text='', filename='', content=None, disposition='business', previous=None):
    return record_review(db, admin, row.id, ReviewInput(source_digest=fingerprint(row), expected_review_id=previous,
        attachment_index=index, disposition=disposition, content_reviewed=True, text=text, filename=filename,
        content_digest=sha256(content).hexdigest() if content is not None else None))


@pytest.mark.parametrize('level', [AccessLevel.VIEW, AccessLevel.EDIT, AccessLevel.FULL])
def test_typed_call_metadata_and_reviewed_note_are_readable_without_raw_body(db, make_user, api, monkeypatch, tmp_path, level):
    admin, card, note, call, file, alias, path, content = setup(db, make_user, monkeypatch, tmp_path)
    user = make_user(Module.CLIENTS, Module.AI, level=level)
    original = deepcopy([row.payload for row in (note, call, file)])
    before = service.history(db, service.get(db, card['id']), user=user)
    calls = [event for event in before['items'] if event['source'] == 'amocrm']
    assert len(calls) == 1 and calls[0]['details']['params'] == {'phone': '+79001112233', 'duration': 54}
    assert 'PASSPORT_MARKER' not in json.dumps(before)
    assert 'PRIVATE_AUDIO' not in json.dumps(before)
    result = classify(db, admin, note, text='Discussed the business floor plan')
    assert result['review_id']
    for value in (api(user).get(f"/api/crm/entries/{card['id']}/history").json(), _read_local_sales_card(db, user, card['id'])):
        serialized = json.dumps(value, default=str)
        assert 'Discussed the business floor plan' in serialized
        assert 'PASSPORT_MARKER' not in serialized and 'HIDDEN_VALUE' not in serialized
    assert original == [row.payload for row in (note, call, file)]
    assert 'PASSPORT_MARKER' in json.dumps(service.history(db, service.get(db, card['id']), user=admin))
    note.payload = {**note.payload, 'params': {'text': 'Changed original'}}
    db.commit()
    assert 'Discussed the business floor plan' not in json.dumps(service.history(db, service.get(db, card['id']), user=user))


def test_approved_file_list_search_download_ai_alias_revoke_and_changed_bytes(db, make_user, api, monkeypatch, tmp_path):
    admin, card, note, call, file, alias, path, content = setup(db, make_user, monkeypatch, tmp_path)
    user = make_user(Module.CLIENTS, Module.AI, level=AccessLevel.FULL)
    client = api(user)
    routes = [f'/api/crm/files/{file.id}/download', f'/files/{alias.id}']
    for route in routes:
        assert client.get(route).status_code == 403
    approved = classify(db, admin, file, index=0, filename='Business plan.txt', content=content)
    listing = client.get('/api/crm/files').json()
    assert listing['items'] == [{'id': file.id, 'name': 'Business plan.txt', 'size': len(content), 'archived': False}]
    assert client.get('/api/crm/files', params={'query': 'PRIVATE'}).json()['total'] == 0
    assert client.get('/api/crm/files', params={'query': 'Business'}).json()['total'] == 1
    for route in routes:
        response = client.get(route)
        assert response.status_code == 200 and response.content == content
        assert 'PRIVATE' not in str(response.headers) and response.headers['cache-control'] == 'private, no-store'
    assert content.decode() in _read_imported_attachment(db, user, file.id)['text']
    assert content.decode() in _read_attached_file(db, user, alias.id)['text']
    for route in [f'/api/moysklad/records/{file.id}', f'/api/moysklad/records/{file.id}/files/0']:
        assert client.get(route).status_code == 403
    path.write_bytes(b'Changed file PRIVATE_REPLACEMENT')
    for route in routes:
        assert client.get(route).status_code == 403
    for operation in (lambda: _read_imported_attachment(db, user, file.id), lambda: _read_attached_file(db, user, alias.id)):
        with pytest.raises(HTTPException) as blocked:
            operation()
        assert blocked.value.status_code == 403
    assert client.get('/api/crm/files').json()['items'] == []
    path.write_bytes(content)
    classify(db, admin, file, index=0, disposition='restricted', previous=approved['review_id'])
    assert all(client.get(route).status_code == 403 for route in routes)
    assert db.query(CRMImportReview).filter_by(source_record_id=file.id).count() == 2
    assert api(admin).get(routes[0]).content == content


def test_only_admin_classifies_and_payload_cannot_self_authorize(db, make_user, api, monkeypatch, tmp_path):
    admin, card, note, call, file, alias, path, content = setup(db, make_user, monkeypatch, tmp_path)
    manager = make_user(Module.CLIENTS, level=AccessLevel.FULL)
    endpoint = f'/api/crm/import-reviews/{note.id}'
    payload = {'source_digest': fingerprint(note), 'disposition': 'business', 'content_reviewed': True, 'text': 'Business note'}
    assert api(manager).get(endpoint).status_code == 403
    assert api(manager).post(endpoint, json=payload).status_code == 403
    assert db.query(CRMImportReview).count() == 0
    response = api(manager).patch(f"/api/crm/entries/{card['id']}", json={'version': card['version'], 'data': {'import_review': payload}})
    assert response.status_code == 422
    note.payload = {**note.payload, 'visibility': 'business', 'reviewed': True, 'public_data': {'text': 'FAKE_APPROVAL'}}
    db.commit()
    assert 'FAKE_APPROVAL' not in json.dumps(service.history(db, service.get(db, card['id']), user=manager))
    client = api(admin)
    stale = client.post(endpoint, json=payload)
    assert stale.status_code == 409
    payload['source_digest'] = fingerprint(note)
    payload['text'] = 'Паспорт нельзя открыть этим способом'
    assert client.post(endpoint, json=payload).status_code == 422
    payload['text'] = 'Verified business note'
    approved = client.post(endpoint, json=payload)
    assert approved.status_code == 201
    assert client.post(endpoint, json=payload).status_code == 409
    assert client.get(endpoint).json()['review_id'] == approved.json()['review_id']


def test_explicit_restriction_closes_call_metadata_and_does_not_change_source(db, make_user, monkeypatch, tmp_path):
    admin, card, note, call, file, alias, path, content = setup(db, make_user, monkeypatch, tmp_path)
    user = make_user(Module.CLIENTS)
    original = deepcopy(call.payload)
    classify(db, admin, call, disposition='restricted')
    history = service.history(db, service.get(db, card['id']), user=user)
    assert not any(event['source'] == 'amocrm' for event in history['items'])
    assert call.payload == original


@pytest.mark.parametrize('restriction', ['accounting', 'production', 'restricted_import', 'unknown_import'])
def test_stricter_file_alias_applies_to_crm_list_download_and_both_ai_paths(db, make_user, api, monkeypatch, tmp_path, restriction):
    admin, card, note, call, file, alias, path, content = setup(db, make_user, monkeypatch, tmp_path)
    user = make_user(Module.CLIENTS, Module.AI)
    classify(db, admin, file, index=0, filename='Business plan.txt', content=content)
    if restriction in ('accounting', 'production'):
        alias.purpose = FilePurpose.MONEY_MOVEMENT_DOCUMENT if restriction == 'accounting' else FilePurpose.KR_PAGE_IMAGE
        db.commit()
    else:
        duplicate = SourceRecord(entity_type='amocrm/files', source_id='duplicate', title='Same bytes', search_text='',
            archived=False, payload=deepcopy(file.payload))
        db.add(duplicate)
        db.commit()
        if restriction == 'restricted_import':
            classify(db, admin, duplicate, index=0, disposition='restricted')
    client = api(user)
    assert client.get(f'/api/crm/files/{file.id}/download').status_code == 403
    assert client.get('/api/crm/files').json()['total'] == 0
    assert client.get(f'/files/{alias.id}').status_code == 403
    for operation in (lambda: _read_imported_attachment(db, user, file.id), lambda: _read_attached_file(db, user, alias.id)):
        with pytest.raises(HTTPException) as blocked:
            operation()
        assert blocked.value.status_code == 403


@pytest.mark.parametrize('change', ['delete_source', 'change_path', 'change_payload', 'different_attachment'])
def test_shared_alias_and_ai_cannot_bypass_missing_or_changed_provenance(db, make_user, api, monkeypatch, tmp_path, change):
    admin, card, note, call, file, alias, path, content = setup(db, make_user, monkeypatch, tmp_path)
    user = make_user(Module.CLIENTS, Module.AI)
    if change != 'delete_source':
        classify(db, admin, file, index=0, filename='Business plan.txt', content=content)
    if change == 'delete_source':
        db.delete(file)
    elif change == 'change_path':
        file.payload = {**file.payload, '_downloaded_files': [{**file.payload['_downloaded_files'][0], 'local_name': 'another-file'}]}
    elif change == 'change_payload':
        file.payload = {**file.payload, 'new_unreviewed': 'changed'}
    else:
        file.payload = {**file.payload, '_downloaded_files': [*file.payload['_downloaded_files'], file.payload['_downloaded_files'][0]]}
    db.commit()
    assert api(user).get(f'/files/{alias.id}').status_code == 403
    with pytest.raises(HTTPException) as blocked:
        _read_attached_file(db, user, alias.id)
    assert blocked.value.status_code == 403


def test_migration_preserves_existing_rows_and_refuses_to_erase_nonempty_review_history():
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, text
    migration_path = Path(__file__).resolve().parents[1] / 'alembic' / 'versions' / 'cf0511202601_crm_import_reviews.py'
    spec = importlib.util.spec_from_file_location('review_migration_test', migration_path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine('sqlite://')
    with engine.begin() as connection:
        connection.execute(text('CREATE TABLE users (id INTEGER PRIMARY KEY)'))
        connection.execute(text('CREATE TABLE moysklad_records (id INTEGER PRIMARY KEY, payload TEXT)'))
        connection.execute(text('INSERT INTO users (id) VALUES (1)'))
        connection.execute(text("INSERT INTO moysklad_records (id,payload) VALUES (1,'unchanged-source')"))
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        assert connection.execute(text('SELECT COUNT(*) FROM crm_import_reviews')).scalar() == 0
        migration.downgrade()
        migration.upgrade()
        connection.execute(text('INSERT INTO crm_import_reviews (source_record_id,attachment_index,disposition,source_digest,public_data,actor_id) VALUES (:source,-1,:state,:digest,:data,1)'),
            {'source': 1, 'state': 'restricted', 'digest': '0' * 64, 'data': '{}'})
        with pytest.raises(RuntimeError, match='not empty'):
            migration.downgrade()
        assert connection.execute(text('SELECT COUNT(*) FROM crm_import_reviews')).scalar() == 1
        assert connection.execute(text('SELECT payload FROM moysklad_records')).scalar() == 'unchanged-source'
    engine.dispose()
