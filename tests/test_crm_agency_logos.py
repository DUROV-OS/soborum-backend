from copy import deepcopy
from hashlib import sha256
from io import BytesIO

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image, PngImagePlugin

from app.common.files import FileAsset, FilePurpose
from app.common.module_access import AccessLevel, Module
from app.crm import agency_logos as logos, service
from app.crm.models import CRMAgencyLogo, CRMEntity, CRMEvent
from app.crm.schemas import Entry, Patch


def picture(format='PNG', size=(32, 18), color=(31, 112, 150), **options):
    output = BytesIO()
    with Image.new('RGB', size, color) as image:
        image.save(output, format=format, **options)
    return output.getvalue()


@pytest.fixture
def setup(db, make_user):
    owner = make_user(admin=True)
    other = make_user(Module.CLIENTS)
    pipeline = service.create(db, 'pipeline', Entry(name='Synthetic agencies', data={'purpose': 'agency'}), owner)
    stage = service.create(db, 'stage', Entry(name='Synthetic new', data={'pipeline_id': pipeline['id']}), owner)
    contact = service.create(db, 'contact', Entry(name='Synthetic contact', data={'phone': '+79000000001'}), owner)
    record = service.create(db, 'deal', Entry(name='Synthetic agency', data={'pipeline_id': pipeline['id'],
        'stage_id': stage['id'], 'contact_ids': [contact['id']], 'responsible_user_id': other.id,
        'agency_profile': {'legal_status': 'ooo', 'members': [{'contact_id': contact['id'], 'roles': ['owner', 'agent']}]}}), owner)
    return owner, record


def upload(client, record, data=None, **headers):
    return client.put(f"/api/crm/agencies/{record['id']}/logo", content=data if data is not None else picture(),
                      headers={'Content-Type': 'image/png', 'If-Match': f'"{record["version"]}"', **headers})


@pytest.mark.parametrize('format', ['PNG', 'JPEG', 'WEBP'])
def test_detects_decodes_and_reencodes_real_rasters(db, api, setup, format):
    owner, record = setup
    before = deepcopy(db.get(CRMEntity, record['id']).payload)
    # MIME/extension are not trusted: JPEG/WebP also arrive with image/png here.
    client = api(owner)
    response = upload(client, record, picture(format))
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved['entry']['version'] == record['version'] + 1
    assert saved['entry']['data'] == before
    assert saved['logo']['width'] == 32 and saved['logo']['height'] == 18
    assert 'image_data' not in saved['logo'] and 'file_id' not in saved['logo'] and 'path' not in saved['logo']
    content = client.get(f"/api/crm/agencies/{record['id']}/logo/content")
    assert content.status_code == 200 and content.headers['content-type'] == 'image/png'
    assert content.headers['cache-control'] == 'private, no-store'
    assert content.headers['x-content-type-options'] == 'nosniff'
    with Image.open(BytesIO(content.content)) as image:
        image.load()
        assert image.format == 'PNG' and image.size == (32, 18) and not image.info
    assert sha256(content.content).hexdigest() == saved['logo']['sha256']
    assert db.query(CRMAgencyLogo).count() == 1 and db.query(FileAsset).count() == 0
    assert db.get(CRMEntity, record['id']).payload == before


def test_orientation_dimensions_metadata_and_appended_bytes_removed(api, setup):
    owner, record = setup
    exif = Image.Exif()
    exif[274] = 6
    exif[270] = 'SYNTHETIC_PRIVATE_METADATA'
    client = api(owner)
    result = upload(client, record, picture('JPEG', size=(2048, 1024), exif=exif)).json()
    assert (result['logo']['width'], result['logo']['height']) == (512, 1024)
    content = client.get(f"/api/crm/agencies/{record['id']}/logo/content").content
    with Image.open(BytesIO(content)) as image:
        assert not image.info and not image.getexif()
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text('secret', 'SYNTHETIC_PRIVATE_METADATA')
    data = picture(pnginfo=metadata) + b'<script>SYNTHETIC_PRIVATE_METADATA</script>'
    result = upload(client, result['entry'], data)
    assert result.status_code == 200
    content = client.get(f"/api/crm/agencies/{record['id']}/logo/content").content
    assert b'SYNTHETIC_PRIVATE_METADATA' not in content and b'<script>' not in content
    with Image.open(BytesIO(content)) as image:
        assert not image.info


@pytest.mark.parametrize('data,status', [
    (b'', 422), (b'<svg xmlns="http://www.w3.org/2000/svg"><script>bad()</script></svg>', 422),
    (b'<html>not an image</html>', 422), (b'%PDF-1.7 synthetic contract', 422),
    (b'\x89PNG\r\n\x1a\ntruncated', 422), (b'x' * (logos.MAX_FILE_BYTES + 1), 413),
    (picture('GIF'), 422), (picture(size=(4097, 1)), 413), (picture(size=(3000, 3000)), 413),
], ids=['empty', 'svg', 'html', 'pdf', 'truncated', 'oversized-body', 'gif', 'oversized-side', 'oversized-pixels'])
def test_invalid_content_never_changes_agency_or_existing_logo(db, api, setup, data, status):
    owner, record = setup
    client = api(owner)
    saved = upload(client, record).json()
    row = db.get(CRMEntity, record['id'])
    before, version = deepcopy(row.payload), row.version
    old = db.get(CRMAgencyLogo, record['id']).image_data
    result = upload(client, saved['entry'], data)
    assert result.status_code == status, result.text
    db.refresh(row)
    assert row.payload == before and row.version == version
    assert db.get(CRMAgencyLogo, row.id).image_data == old


def test_animated_png_rejected(api, setup):
    owner, record = setup
    output = BytesIO()
    with Image.new('RGB', (4, 4), 'red') as first, Image.new('RGB', (4, 4), 'blue') as second:
        first.save(output, format='PNG', save_all=True, append_images=[second], duration=100, loop=0)
    assert upload(api(owner), record, output.getvalue()).status_code == 422


@pytest.mark.parametrize('level', [AccessLevel.NONE, AccessLevel.VIEW, AccessLevel.EDIT, AccessLevel.FULL])
def test_permissions_are_crm_levels_without_owner_only_restrictions(db, api, make_user, setup, level):
    owner, record = setup
    saved = upload(api(owner), record).json()
    worker = make_user(Module.CLIENTS, Module.AI, level=level)
    assert worker.id != record['data']['responsible_user_id']
    client = api(worker)
    path = f"/api/crm/agencies/{record['id']}/logo"
    assert client.get(path).status_code == (200 if level >= AccessLevel.VIEW else 403)
    assert client.get(path + '/content').status_code == (200 if level >= AccessLevel.VIEW else 403)
    response = upload(client, saved['entry'], picture(color='blue'))
    assert response.status_code == (200 if level >= AccessLevel.EDIT else 403)
    current = response.json()['entry'] if response.status_code == 200 else saved['entry']
    removed = client.request('DELETE', path, json={'version': current['version']})
    assert removed.status_code == (200 if level == AccessLevel.FULL else 403)


def test_ai_only_cannot_access_logo_and_there_is_no_shared_file_alias(db, api, make_user, setup):
    owner, record = setup
    upload(api(owner), record)
    ai = make_user(Module.AI, level=AccessLevel.FULL)
    client = api(ai)
    path = f"/api/crm/agencies/{record['id']}/logo"
    assert client.get(path).status_code == client.get(path + '/content').status_code == 403
    assert client.get(path + '/content?token=synthetic').status_code == 403
    assert upload(client, record).status_code == 403
    assert db.query(FileAsset).count() == 0
    # The blob is in PostgreSQL/SQLite, never on the application filesystem.
    assert not hasattr(db.get(CRMAgencyLogo, record['id']), 'path_on_disk')
    from app.ai.tools import _read_attached_file
    assert _read_attached_file(db, ai, record['id']) == {'error': 'Файл не найден'}
    assert client.get(f"/files/{record['id']}").status_code == 404


def test_no_token_in_url_authentication():
    from app.main import app
    client = TestClient(app)
    assert client.get('/api/crm/agencies/1/logo/content?token=synthetic').status_code == 401


def test_version_conflict_replace_remove_and_normal_edits_preserve_payload(db, api, setup):
    owner, record = setup
    client = api(owner)
    saved = upload(client, record).json()
    path = f"/api/crm/agencies/{record['id']}/logo"
    assert upload(client, record, picture(color='green')).status_code == 409
    assert client.request('DELETE', path, json={'version': record['version']}).status_code == 409
    newer = service.patch(db, record['id'], Patch(version=saved['entry']['version'], name='Changed agency'), owner)
    assert logos.status(db, owner, record['id'])['logo']['sha256'] == saved['logo']['sha256']
    assert upload(client, saved['entry']).status_code == 409
    replaced = upload(client, newer, picture(color='green')).json()
    assert replaced['logo']['sha256'] != saved['logo']['sha256']
    assert replaced['entry']['data'] == record['data']
    assert replaced['entry']['name'] == 'Changed agency'
    assert client.get(path + '/content', headers={'If-Match': '"' + saved['logo']['sha256'] + '"'}).status_code == 412
    assert client.request('DELETE', path, json={'version': replaced['entry']['version']}).status_code == 200
    assert client.get(path).json()['logo'] is None
    assert client.get(path + '/content').status_code == 404
    assert db.query(CRMAgencyLogo).count() == 0
    assert db.get(CRMEntity, record['id']).payload == record['data']


def test_wrong_kind_pipeline_archive_and_arbitrary_file_binding_are_blocked(db, api, setup, tmp_path):
    owner, record = setup
    client = api(owner)
    saved = upload(client, record).json()
    foreign = tmp_path / 'synthetic-financial-document.pdf'
    foreign.write_bytes(b'%PDF-1.7 SYNTHETIC_PRIVATE')
    asset = FileAsset(filename='financial.pdf', content_type='application/pdf', path_on_disk=str(foreign),
                      purpose=FilePurpose.CONTRACT, uploaded_by_id=owner.id)
    db.add(asset)
    db.commit()
    for key in ('logo_file_id', 'logo_id', 'logo', 'path_on_disk'):
        bad = client.patch(f"/api/crm/entries/{record['id']}", json={'version': saved['entry']['version'],
            'data': {'agency_profile': {key: asset.id}}})
        assert bad.status_code == 422
    path = f"/api/crm/agencies/{record['id']}/logo"
    assert client.put(path, json={'version': saved['entry']['version'], 'file_id': asset.id}).status_code == 415
    assert client.put(path, content=picture(), headers={'Content-Type': 'image/png'}).status_code == 428
    other = service.create(db, 'contact', Entry(name='Not an agency'), owner)
    assert upload(client, other).status_code == 404
    archived = service.patch(db, record['id'], Patch(version=saved['entry']['version'], archived=True), owner)
    assert upload(client, archived).status_code == 409
    assert client.get(path + '/content').status_code == 200
    pipeline = db.get(CRMEntity, record['data']['pipeline_id'])
    pipeline.payload = {**pipeline.payload, 'purpose': 'sales'}
    db.commit()
    assert client.get(path + '/content').status_code == 404
    assert db.query(CRMAgencyLogo).count() == 1


def test_failed_commit_does_not_lose_old_logo_or_change_version(db, api, setup, monkeypatch):
    owner, record = setup
    saved = upload(api(owner), record).json()
    previous = db.get(CRMAgencyLogo, record['id']).image_data
    def fail():
        raise RuntimeError('synthetic database failure')
    monkeypatch.setattr(db, 'commit', fail)
    with pytest.raises(RuntimeError, match='synthetic database failure'):
        logos.save(db, owner, record['id'], saved['entry']['version'], logos.normalize(picture(color='blue')))
    assert db.get(CRMAgencyLogo, record['id']).image_data == previous
    assert db.get(CRMEntity, record['id']).version == saved['entry']['version']


def test_chunked_body_limit_does_not_trust_content_length(api, setup):
    owner, record = setup
    def stream():
        for _ in range(6):
            yield b'x' * (1024 * 1024)
    response = api(owner).put(f"/api/crm/agencies/{record['id']}/logo", content=stream(),
        headers={'Content-Type': 'image/png', 'If-Match': f'"{record["version"]}"'})
    assert response.status_code == 413
