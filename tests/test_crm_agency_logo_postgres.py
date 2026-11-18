"""Real PostgreSQL concurrency/migration checks, restricted to a local scratch DB."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from hashlib import sha256
from io import BytesIO
import os
import json
import subprocess
import sys
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi import HTTPException
from PIL import Image
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.crm import agency_logos as logos, service
from app.crm.models import CRMAgencyLogo, CRMEntity, CRMEvent
from app.crm.schemas import Entry, Patch
from app.users.models import User, UserRole

TEST_URL = os.getenv('CRM_LOGO_TEST_DATABASE_URL', '')
pytestmark = pytest.mark.skipif(not TEST_URL, reason='Requires explicitly isolated local logo PostgreSQL')


@pytest.fixture
def pg_schema():
    url = make_url(TEST_URL)
    assert url.host in ('127.0.0.1', 'localhost') and url.database == 'crm_logo_test'
    schema = 'crm_logo_' + uuid4().hex
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        yield url.update_query_dict({'options': f'-csearch_path={schema}'})
    finally:
        assert schema.startswith('crm_logo_') and schema.replace('_', '').isalnum()
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()


@pytest.fixture
def db(pg_schema):
    from app.db.base import Base
    engine = create_engine(pg_schema)
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session
    engine.dispose()


def seed(db):
    actor = User(email='logo-synthetic@example.invalid', full_name='Synthetic',
                 hashed_password='not-a-password', role=UserRole.ADMIN, is_active=True)
    db.add(actor)
    db.commit()
    pipe = service.create(db, 'pipeline', Entry(name='Synthetic agency pipeline', data={'purpose': 'agency'}), actor)
    stage = service.create(db, 'stage', Entry(name='Synthetic new', data={'pipeline_id': pipe['id']}), actor)
    agency = service.create(db, 'deal', Entry(name='Synthetic agency', data={
        'pipeline_id': pipe['id'], 'stage_id': stage['id'], 'amount': 1234,
        'agency_profile': {'legal_status': 'ooo', 'members': []}}), actor)
    output = BytesIO()
    with Image.new('RGBA', (32, 18), (30, 120, 200, 128)) as image:
        image.save(output, format='PNG')
    return actor, agency, logos.normalize(output.getvalue())


@pytest.mark.parametrize('second', ['upload', 'ordinary-edit'])
def test_concurrent_writes_accept_exactly_one_current_version(db, second):
    actor, agency, image = seed(db)
    actor_id = actor.id
    before = deepcopy(agency['data'])
    barrier = Barrier(2)
    def mutate(index):
        with Session(db.get_bind()) as session:
            user = session.get(User, actor_id)
            barrier.wait(timeout=10)
            try:
                if index and second == 'ordinary-edit':
                    service.patch(session, agency['id'], Patch(version=agency['version'], name='Synthetic changed'), user)
                else:
                    logos.save(session, user, agency['id'], agency['version'], image)
                return 200
            except HTTPException as error:
                session.rollback()
                return error.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(mutate, range(2))) == [200, 409]
    db.expire_all()
    row = db.get(CRMEntity, agency['id'])
    assert row.version == agency['version'] + 1 and row.payload == before
    count = db.query(CRMAgencyLogo).count()
    assert count == (1 if second == 'upload' or row.name == agency['name'] else 0)
    assert db.query(CRMEvent).filter(CRMEvent.action == 'agency_logo_updated').count() == count
    if count:
        stored = db.get(CRMAgencyLogo, row.id)
        assert stored.image_data == image.data and stored.sha256 == sha256(image.data).hexdigest()


def test_real_migration_roundtrip_and_nonempty_rollback_guard(pg_schema, monkeypatch):
    from alembic import command
    from alembic.config import Config
    from app.core.config import settings
    monkeypatch.setattr(settings, 'database_url', pg_schema.render_as_string(hide_password=False).replace('%3D', '='))
    config = Config(str(Path(__file__).resolve().parents[1] / 'alembic.ini'))
    command.upgrade(config, 'cf0511202601')
    engine = create_engine(pg_schema)
    with engine.connect() as conn:
        tables = inspect(conn).get_table_names()
        before = {table: conn.execute(text(f'SELECT COUNT(*) FROM "{table}"')).scalar() for table in tables if table != 'alembic_version'}
    command.upgrade(config, 'cg0611202601')
    with engine.connect() as conn:
        assert {table: conn.execute(text(f'SELECT COUNT(*) FROM "{table}"')).scalar() for table in before} == before
        assert conn.execute(text('SELECT COUNT(*) FROM crm_agency_logos')).scalar() == 0
        columns = {column['name']: str(column['type']) for column in inspect(conn).get_columns('crm_agency_logos')}
        assert columns['image_data'] == 'BYTEA'
    command.downgrade(config, 'cf0511202601')
    command.upgrade(config, 'cg0611202601')
    with Session(engine) as session:
        actor, agency, image = seed(session)
        logos.save(session, actor, agency['id'], agency['version'], image)
    with pytest.raises(RuntimeError, match='not empty'):
        command.downgrade(config, 'cf0511202601')
    with engine.connect() as conn:
        assert conn.execute(text('SELECT version_num FROM alembic_version')).scalar() == 'cg0611202601'
        assert bytes(conn.execute(text('SELECT image_data FROM crm_agency_logos')).scalar()) == image.data
    engine.dispose()


def test_previous_application_edits_agency_without_losing_new_logo(db, pg_schema):
    fallback = os.getenv('CRM_LOGO_FALLBACK_SOURCE', '')
    if not fallback:
        pytest.skip('Requires the reviewed previous-application checkout')
    assert (Path(fallback) / 'app/crm/service.py').is_file()
    assert not (Path(fallback) / 'app/crm/agency_logos.py').exists()
    actor, agency, image = seed(db)
    current = logos.save(db, actor, agency['id'], agency['version'], image)['entry']
    expected = deepcopy(current['data'])
    probe = '''import json, os
from hashlib import sha256
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from app.crm import service
from app.crm.schemas import Patch
from app.users.models import User
from app.db import import_all_models
assert not hasattr(__import__('app.crm.models', fromlist=['CRMAgencyLogo']), 'CRMAgencyLogo')
with Session(create_engine(os.environ['DATABASE_URL'])) as db:
 before = bytes(db.execute(text('SELECT image_data FROM crm_agency_logos')).scalar())
 row = service.patch(db, int(os.environ['TEST_AGENCY_ID']), Patch(version=int(os.environ['TEST_AGENCY_VERSION']), name='Fallback edit'), db.get(User, int(os.environ['TEST_ACTOR_ID'])))
 after = bytes(db.execute(text('SELECT image_data FROM crm_agency_logos')).scalar())
 assert before == after
 print(json.dumps({'version':row['version'], 'data':row['data'], 'sha256':sha256(after).hexdigest()}))
'''
    env = {**os.environ, 'DATABASE_URL': pg_schema.render_as_string(hide_password=False),
           'PYTHONPATH': str(Path(fallback).resolve()), 'TEST_AGENCY_ID': str(agency['id']),
           'TEST_AGENCY_VERSION': str(current['version']), 'TEST_ACTOR_ID': str(actor.id)}
    result = subprocess.run([sys.executable, '-c', probe], cwd=fallback, env=env,
                            capture_output=True, text=True, timeout=60, check=True)
    checked = json.loads(result.stdout.strip().splitlines()[-1])
    assert checked == {'version': current['version'] + 1, 'data': expected, 'sha256': sha256(image.data).hexdigest()}
    db.expire_all()
    assert db.get(CRMEntity, agency['id']).name == 'Fallback edit'
    assert logos.content(db, actor, agency['id']).body == image.data
