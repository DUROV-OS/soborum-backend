"""Concurrency and additive migration tests, only in an explicit scratch DB."""
from concurrent.futures import ThreadPoolExecutor
from importlib.util import spec_from_file_location, module_from_spec
import os
from pathlib import Path
from threading import Barrier
from uuid import uuid4
import httpx
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import HTTPException
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from app.db.base import Base
from app.users.models import User, UserRole
from app.crm.models import CRMEntity
from app.telephony import routing, provider
from app.telephony.models import PhoneRoutingMemory, PhoneRoutingConfig

URL = os.getenv('COMMUNICATIONS_TEST_DATABASE_URL', '')
pytestmark = pytest.mark.skipif(not URL, reason='Requires isolated local communications_test PostgreSQL')
TABLES = {'max_inbound_messages', 'phone_calls', 'phone_call_audit', 'phone_routing_config', 'phone_routing_members',
          'phone_routing_memory', 'phone_routing_audit', 'phone_dial_requests'}


@pytest.fixture
def engine():
    url = make_url(URL)
    assert url.host in ('localhost', '127.0.0.1') and url.database == 'communications_test'
    parent = create_engine(url)
    schema = 'communications_' + uuid4().hex
    with parent.begin() as conn:
        conn.execute(text('CREATE SCHEMA "' + schema + '"'))
    engine = create_engine(url.update_query_dict({'options': '-csearch_path=' + schema}))
    try:
        yield engine
    finally:
        engine.dispose()
        assert schema.startswith('communications_') and schema.replace('_', '').isalnum()
        with parent.begin() as conn:
            conn.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))
        parent.dispose()


def seed(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        users = [User(email=str(i) + '@synthetic.invalid', full_name='Manager ' + str(i),
                      hashed_password='not-a-login', is_active=True, role=UserRole.ADMIN) for i in range(2)]
        db.add_all(users); db.commit()
        ids = [user.id for user in users]
        routing.save_config(db, routing.Configuration(version=1, enabled=True, pbx_setup_confirmed=True,
            members=[routing.Member(user_id=ids[0], pbx_user='101'), routing.Member(user_id=ids[1], pbx_user='102')]), users[0])
    return ids


def test_simultaneous_new_and_repeated_callers_are_fair_and_idempotent(engine):
    seed(engine)
    barrier = Barrier(8)
    def contact(index):
        with Session(engine) as db:
            barrier.wait(timeout=10)
            return routing.contact(db, {'phone': '7900111' + str(index).zfill(4)})['responsible']
    with ThreadPoolExecutor(max_workers=8) as pool:
        assignments = list(pool.map(contact, range(16)))
        repeated = list(pool.map(contact, [99] * 8))
    assert assignments.count('101') == assignments.count('102') == 8
    assert len(set(repeated)) == 1
    with Session(engine) as db:
        assert db.get(PhoneRoutingConfig, 1).cursor == 17
        assert db.query(PhoneRoutingMemory).count() == 17


def test_concurrent_admin_edits_keep_one_version(engine):
    ids = seed(engine)
    barrier = Barrier(2)
    with Session(engine) as db:
        value = routing.read_config(db)
        value.pop('fallback')
    def save(wait):
        with Session(engine) as db:
            barrier.wait(timeout=10)
            try:
                routing.save_config(db, routing.Configuration(**{**value, 'wait_seconds': wait}), db.get(User, ids[0]))
                return 200
            except HTTPException as error:
                db.rollback()
                return error.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(save, [20, 30])) == [200, 409]


def test_concurrent_dial_replays_never_start_twice(engine, monkeypatch):
    ids = seed(engine)
    barrier = Barrier(8)
    calls = []
    key = uuid4()
    monkeypatch.setattr(provider, 'configured', lambda: True)
    monkeypatch.setattr(provider, 'request', lambda *a, **kw: calls.append(kw) or httpx.Response(200))
    def dial(_):
        with Session(engine) as db:
            user = db.get(User, ids[0])
            barrier.wait(timeout=10)
            return provider.dial(db, user, key, '79001112233')['state']
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert set(pool.map(dial, range(8))) <= {'requested', 'pending'}
    assert len(calls) == 1


def test_migration_upgrade_downgrade_preserves_crm_and_starts_disabled(engine):
    Base.metadata.create_all(engine, tables=[t for t in Base.metadata.sorted_tables if t.name not in TABLES])
    with Session(engine) as db:
        db.add(CRMEntity(kind='contact', name='Before migration', payload={'phone': '79001112233', 'customer': {'phone_2': '79002223344'}}))
        db.commit()
    spec = spec_from_file_location('communications_migration', Path(__file__).parents[1] / 'alembic/versions/ch0911202601_communications.py')
    module = module_from_spec(spec); spec.loader.exec_module(module)
    assert module.down_revision == 'cg0611202601'
    with engine.begin() as conn:
        module.op = Operations(MigrationContext.configure(conn))
        before = conn.execute(text("SELECT id,name,payload::text FROM crm_entities")).all()
        module.upgrade()
        assert TABLES <= set(inspect(conn).get_table_names())
        assert conn.execute(text("SELECT enabled,main_phone,cursor FROM phone_routing_config")).one() == (False, '79066773555', 0)
        module.downgrade()
        assert not TABLES.intersection(inspect(conn).get_table_names())
        assert conn.execute(text("SELECT id,name,payload::text FROM crm_entities")).all() == before
        module.upgrade()
        assert conn.execute(text("SELECT id,name,payload::text FROM crm_entities")).all() == before


def test_concurrent_max_delivery_and_manual_creation_are_idempotent(engine):
    from datetime import datetime, timedelta, timezone
    from app.crm import max_intake, chat_links
    from app.crm.chat_models import CRMChatLink
    from app.crm.models import CRMEvent
    from app.telephony.models import MAXInboundMessage
    from app.notifications.models import Notification
    ids = seed(engine)
    with Session(engine) as db:
        for user in db.query(User): user.job_role = 'manager'
        pipeline = CRMEntity(kind='pipeline', name='Продажи', payload={'purpose': 'sales'})
        db.add(pipeline); db.flush()
        stage = CRMEntity(kind='stage', name='Новые заявки', payload={'pipeline_id': pipeline.id, 'outcome': 'open'})
        db.add(stage); db.flush()
        config = db.get(PhoneRoutingConfig, 1)
        config.max_incoming_enabled, config.lead_stage_id, config.lead_actor_id = True, stage.id, ids[0]
        config.max_leads_started_at = datetime.now(timezone.utc) - timedelta(seconds=60)
        db.commit()
    barrier = Barrier(8)
    def receive(i):
        with Session(engine) as db:
            barrier.wait(timeout=10)
            max_intake.ingest(db, {'account_id':'1','chat_id':3,'phone':'79001112233','name':'Клиент'},
                {'id':str(i % 2), 'sender':2, 'time':int(datetime.now(timezone.utc).timestamp()*1000), 'text':'Расчёт'})
            db.commit()
    with ThreadPoolExecutor(max_workers=8) as pool: list(pool.map(receive, range(8)))
    with Session(engine) as db:
        assert db.query(MAXInboundMessage).count() == 2
        assert db.query(Notification).count() == 2
        assert db.query(CRMEntity).filter_by(kind='deal').count() == 0
    def attach(i):
        with Session(engine) as db:
            barrier.wait(timeout=10)
            return chat_links.create_link(db, chat_links.LinkFromMax(max_chat_id=3,
                create=chat_links.CreateCard(kind='deal', name='Клиент', phone='79001112233')), db.get(User, ids[i % 2]))['entity_id']
    with ThreadPoolExecutor(max_workers=8) as pool: results = list(pool.map(attach, range(8)))
    assert len(set(results)) == 1
    with Session(engine) as db:
        assert db.query(CRMEntity).filter_by(kind='deal').count() == 1
        assert db.query(CRMEntity).filter_by(kind='contact').count() == 1
        assert db.query(CRMChatLink).count() == 1
        assert db.query(CRMEvent).filter_by(action='max_message_received').count() == 2
        assert db.query(Notification).filter_by(read_at=None).count() == 0
