"""Opt-in PostgreSQL checks, restricted to an explicitly named local test DB."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
from threading import Barrier
from uuid import uuid4
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from fastapi import HTTPException

from app.crm import service
from app.crm.import_access import ReviewInput, fingerprint, record_review
from app.crm.models import CRMEntity, CRMImportReview
from app.crm.schemas import Entry
from app.moysklad.models import SourceRecord
from app.users.models import User, UserRole

TEST_URL = os.getenv('CRM_REVIEW_TEST_DATABASE_URL', '')
pytestmark = pytest.mark.skipif(not TEST_URL, reason='Requires explicitly isolated local CRM review PostgreSQL')


@pytest.fixture
def pg_schema():
    url = make_url(TEST_URL)
    assert url.host in ('127.0.0.1', 'localhost') and url.database == 'crm_review_test'
    schema = 'crm_review_' + uuid4().hex
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    isolated = url.update_query_dict({'options': f'-csearch_path={schema}'})
    try:
        yield isolated
    finally:
        assert schema.startswith('crm_review_') and schema.replace('_', '').isalnum()
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()


@pytest.fixture
def db(pg_schema):
    from app.db.base import Base
    from app.db import import_all_models  # noqa: F401
    engine = create_engine(pg_schema)
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session
    engine.dispose()


def test_pg_correlated_temperature_and_totals_before_pagination(db, make_user):
    user = make_user(admin=True)
    def add(kind, name, **data):
        return service.create(db, kind, Entry(name=name, data=data), user)
    pipeline = add('pipeline', 'Synthetic sales')
    stage = add('stage', 'Synthetic first', pipeline_id=pipeline['id'])
    hot = add('contact', 'Hot', customer={'temperature': 'hot'})
    cold = add('contact', 'Cold', customer={'temperature': 'cold'})
    for index in range(105):
        db.add(CRMEntity(kind='deal', name=f'Synthetic {index}', payload={
            'pipeline_id': pipeline['id'], 'stage_id': stage['id'], 'contact_ids': [hot['id']], 'amount': 100, 'currency': 'RUB'}))
    db.commit()
    linked = add('deal', 'Unsorted', pipeline_id=pipeline['id'], stage_id=stage['id'], is_unsorted=True,
        contact_ids=[hot['id'], cold['id']], primary_contact_id=cold['id'], amount=500)
    add('inquiry', 'Inquiry', deal_id=linked['id'], pipeline_id=pipeline['id'])
    page = service.list_entries(db, 'deal', temperature='hot', offset=100, limit=10, user=user)
    assert page['total'] == 105 and len(page['items']) == 5
    assert service.list_entries(db, 'inquiry', temperature='cold', user=user)['total'] == 1
    assert service.list_entries(db, 'inquiry', temperature='hot', user=user)['total'] == 0
    summary = service.kanban_summary(db, temperature='hot', user=user)
    assert sum(column['count'] for column in summary['columns']) == 105
    assert next(column for column in summary['columns'] if column['stage_id'] == stage['id'])['amounts'] == {'RUB': 10500}


def test_pg_parallel_reviews_require_the_latest_review_id(db, make_user):
    admin = make_user(admin=True)
    source = SourceRecord(entity_type='amocrm/contacts/notes', source_id='synthetic', title='Synthetic',
        search_text='', archived=False, payload={'entity_id': 1, 'note_type': 'common', 'params': {'text': 'Business text'}})
    db.add(source)
    db.commit()
    digest, source_id, admin_id = fingerprint(source), source.id, admin.id
    barrier = Barrier(2)
    def review(_):
        with Session(db.get_bind()) as session:
            actor = session.get(User, admin_id)
            barrier.wait(timeout=10)
            try:
                return record_review(session, actor, source_id, ReviewInput(source_digest=digest,
                    disposition='business', content_reviewed=True, text='Verified business text'))['disposition']
            except HTTPException as error:
                session.rollback()
                return error.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(review, range(2)))
    assert sorted(results, key=str) == [409, 'business']
    assert db.query(CRMImportReview).count() == 1


def test_pg_upgrade_empty_downgrade_reupgrade_and_nonempty_guard(pg_schema, monkeypatch):
    from alembic import command
    from alembic.config import Config
    from app.core.config import settings
    # This fixture's only query option is an ASCII search_path. Keep '=' literal
    # so the project's existing Alembic INI interpolation does not consume '%'.
    monkeypatch.setattr(settings, 'database_url', pg_schema.render_as_string(hide_password=False).replace('%3D', '='))
    config = Config(str(Path(__file__).resolve().parents[1] / 'alembic.ini'))
    command.upgrade(config, 'ce0511202603')
    engine = create_engine(pg_schema)
    with engine.connect() as connection:
        tables = inspect(connection).get_table_names()
        counts = {table: connection.execute(text(f'SELECT COUNT(*) FROM "{table}"')).scalar() for table in tables}
    command.upgrade(config, 'cf0511202601')
    with engine.connect() as connection:
        assert {table: connection.execute(text(f'SELECT COUNT(*) FROM "{table}"')).scalar() for table in tables} == counts
        assert connection.execute(text('SELECT COUNT(*) FROM crm_import_reviews')).scalar() == 0
    command.downgrade(config, 'ce0511202603')
    command.upgrade(config, 'cf0511202601')
    with Session(engine) as session:
        user = User(email='synthetic@example.invalid', full_name='Synthetic administrator', hashed_password='not-a-login-password', role=UserRole.ADMIN)
        source = SourceRecord(entity_type='amocrm/contacts/notes', source_id='synthetic', title='Synthetic original', search_text='', payload={'keep': True})
        session.add_all([user, source])
        session.flush()
        session.add(CRMImportReview(source_record_id=source.id, attachment_index=-1, disposition='restricted', source_digest='0' * 64, actor_id=user.id, public_data={}))
        session.commit()
    with pytest.raises(RuntimeError, match='not empty'):
        command.downgrade(config, 'ce0511202603')
    with engine.connect() as connection:
        assert connection.execute(text('SELECT version_num FROM alembic_version')).scalar() == 'cf0511202601'
        assert connection.execute(text('SELECT COUNT(*) FROM crm_import_reviews')).scalar() == 1
        assert connection.execute(text("SELECT payload->>'keep' FROM moysklad_records WHERE source_id='synthetic'")).scalar() == 'true'
    engine.dispose()
