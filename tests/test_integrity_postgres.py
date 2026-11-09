"""Actual database controls, only in an explicitly named disposable database."""
from concurrent.futures import ThreadPoolExecutor
from importlib.util import module_from_spec, spec_from_file_location
import json
import os
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.db.base import Base
from app.integrity.context import bind_actor
from app.integrity.models import IntegrityEvent
from app.integrity.service import EXPECTED_TABLES, protection, verify
from app.users.models import User, UserRole

URL = os.getenv("INTEGRITY_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="Requires disposable integrity_isolated_test PostgreSQL")


@pytest.fixture(scope="module")
def engine():
    url = make_url(URL)
    assert url.host in {"localhost", "127.0.0.1"} and url.database == "integrity_isolated_test"
    engine = create_engine(url, pool_size=8)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE integrity_events"))
        conn.execute(text("ALTER TABLE file_assets DROP COLUMN sha256, DROP COLUMN size_bytes"))
        for table in ('costing_signoffs','costing_evidence_documents','costing_overhead_plans','costing_approval_policy'):
            conn.execute(text('DROP TABLE '+table))
        covered = set()
        for name in ('oc0911202601_integrity_ledger.py','ca0911202601_costing_approvals.py'):
            filename = Path(__file__).resolve().parents[1] / 'alembic/versions' / name
            spec = spec_from_file_location('migration_'+name, filename)
            migration = module_from_spec(spec); spec.loader.exec_module(migration)
            covered.update(migration.TABLES)
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
        assert covered == EXPECTED_TABLES
    yield engine
    engine.dispose()


def new_user(db):
    user = User(email=str(uuid4()) + "@test.invalid", full_name="Synthetic", role=UserRole.ADMIN,
                hashed_password="secret-must-never-enter-history", is_active=True)
    db.add(user); db.commit()
    return user


def test_real_triggers_cover_bulk_changes_deletions_and_atomic_rollback(engine):
    with Session(engine) as db:
        assert protection(db)["state"] == "active"
        assert json.loads(db.get(IntegrityEvent, 1).payload)["occurred_at"].endswith("Z")
        user = new_user(db)
        bind_actor(db, user)
        uid = user.id
        db.execute(text("UPDATE users SET full_name='First update' WHERE id=:id"), {"id": uid})
        db.commit()
        db.execute(text("UPDATE users SET full_name='Second update' WHERE id=:id"), {"id": uid})
        db.commit()
        before = verify(db)
        db.execute(text("UPDATE users SET full_name='Rolled back' WHERE id=:id"), {"id": uid})
        db.rollback()
        assert verify(db)["checkpoint"] == before["checkpoint"]
        latest = db.scalar(select(IntegrityEvent).order_by(IntegrityEvent.sequence.desc()).limit(1))
        assert json.loads(latest.payload)["actor"]["id"] == uid
        assert "secret-must-never-enter-history" not in latest.payload
        assert json.loads(latest.payload)["before"]["full_name"] == "First update"
        assert json.loads(latest.payload)["after"]["full_name"] == "Second update"
        db.execute(text("DELETE FROM users WHERE id=:id"), {"id": uid}); db.commit()
        latest = db.scalar(select(IntegrityEvent).order_by(IntegrityEvent.sequence.desc()).limit(1))
        assert latest.action == "DELETE" and json.loads(latest.payload)["before"]["id"] == uid
        assert verify(db, before["checkpoint"])["state"] == "consistent"
    with Session(engine) as other:
        new_user(other)
        latest = other.scalar(select(IntegrityEvent).order_by(IntegrityEvent.sequence.desc()).limit(1))
        assert json.loads(latest.payload)["actor"]["kind"] == "system_or_unattributed"


@pytest.mark.parametrize("statement", [
    "UPDATE integrity_events SET payload='{}' WHERE sequence=1",
    "DELETE FROM integrity_events WHERE sequence=1", "TRUNCATE integrity_events",
    "TRUNCATE costing_audit", "TRUNCATE warehouse_materials CASCADE",
])
def test_ordinary_sql_cannot_rewrite_or_truncate_history(engine, statement):
    with Session(engine) as db:
        with pytest.raises(DBAPIError):
            db.execute(text(statement))
        db.rollback()
        assert verify(db)["state"] == "consistent"


def test_concurrent_business_writes_form_one_contiguous_chain(engine):
    barrier = Barrier(6)
    def write(index):
        with Session(engine) as db:
            barrier.wait(timeout=10)
            return new_user(db).id
    with ThreadPoolExecutor(max_workers=6) as pool:
        ids = list(pool.map(write, range(6)))
    assert len(set(ids)) == 6
    with Session(engine) as db:
        assert verify(db)["state"] == "consistent"


def test_owner_tampering_and_missing_trigger_are_reported(engine):
    with Session(engine) as db:
        checkpoint = verify(db)["checkpoint"]
        db.execute(text("ALTER TABLE integrity_events DISABLE TRIGGER integrity_history_immutable"))
        db.execute(text("UPDATE integrity_events SET payload='{}' WHERE sequence=2"))
        assert verify(db)["state"] == "broken"
        db.rollback()
        db.execute(text("ALTER TABLE integrity_events DISABLE TRIGGER integrity_history_immutable"))
        db.execute(text("DELETE FROM integrity_events WHERE sequence=:seq"), {"seq": checkpoint["sequence"]})
        assert verify(db, checkpoint)["state"] == "checkpoint_mismatch"
        db.rollback()
        db.execute(text("ALTER TABLE money_movements DISABLE TRIGGER integrity_capture_change"))
        assert protection(db)["state"] == "incomplete"
        db.rollback()
        assert verify(db, checkpoint)["state"] == "consistent"


def test_signed_costing_records_cannot_be_rewritten_by_sql(engine):
    from datetime import date
    from app.costing.governance_models import CostingOverheadPlan,CostingSignoff
    from app.costing.models import CostingEstimate,CostingRevision
    with Session(engine) as db:
        user=new_user(db)
        plan=CostingOverheadPlan(month=date(2026,11,1),version=1,actor_id=user.id,content={'synthetic':True})
        estimate=CostingEstimate(actor_id=user.id,title='Synthetic protected quote',number='',version=1,status='draft',archived=False)
        db.add_all([plan,estimate]);db.flush()
        revision=CostingRevision(estimate_id=estimate.id,number=1,actor_id=user.id,reason='Synthetic',request_id=str(uuid4()),request_data={},snapshot={})
        db.add(revision);db.flush()
        signoff=CostingSignoff(target='estimate',target_id=estimate.id,stage='production',version=1,revision_id=revision.id,actor_id=user.id,request_id=str(uuid4()),fingerprint='0'*64,content={})
        db.add(signoff);db.commit()
        for table,id in [('costing_overhead_plans',plan.id),('costing_signoffs',signoff.id),('costing_revisions',revision.id)]:
            for statement in (f'UPDATE {table} SET id=id WHERE id=:id',f'DELETE FROM {table} WHERE id=:id'):
                with pytest.raises(DBAPIError): db.execute(text(statement),{'id':id})
                db.rollback()
        db.execute(text('ALTER TABLE costing_signoffs DISABLE TRIGGER costing_version_immutable'))
        assert protection(db)['state']=='incomplete'
        db.rollback()
        assert verify(db)['state']=='consistent'


def test_concurrent_production_signoff_cannot_approve_two_versions(engine):
    from fastapi import HTTPException
    from app.costing.governance_models import CostingApprovalPolicy,CostingSignoff
    from app.costing.governance_router import review_estimate
    from app.costing.governance_schemas import Review
    from app.costing.models import CostingRevision
    from app.costing.router import write_material,write_item,save_estimate
    from app.costing.schemas import MaterialInput,ItemInput,EstimateInput
    with Session(engine) as db:
        owner=new_user(db); operator=new_user(db); accountant=new_user(db)
        rule=db.get(CostingApprovalPolicy,1)
        rule.production_user_id=operator.id;rule.accounting_user_id=accountant.id;rule.director_user_id=owner.id;db.commit()
        mat=write_material(db,owner,MaterialInput(name='Concurrent material',unit='шт',price='50',price_source='Synthetic',effective_on='2026-01-01'))
        expense={k:'0' for k in ('labor','transport','subcontract','overhead','commercial','advertising','taxes','reserve')}|{'evidence':'Synthetic'}
        item=write_item(db,owner,ItemInput(kind='template',title='Concurrent item',content={'lines':[{'material_id':mat['id'],'quantity':'1'}],'expenses':expense,'retail_price':'1000'}))
        quote=save_estimate(db,owner,EstimateInput(request_id=uuid4(),title='Concurrent quote',template_id=item['id']))
        uid=operator.id;eid=quote['id']
    barrier=Barrier(2)
    def approve():
        with Session(engine) as db:
            user=db.get(User,uid);body=Review(version=1,request_id=uuid4(),reason='Synthetic concurrent decision',work_scope='Assembly and inspection')
            barrier.wait(timeout=10)
            try: return ('ok',review_estimate(eid,'production',body,db,user)['version'])
            except HTTPException as error: db.rollback();return ('error',error.status_code)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:approve(),range(2)))
    assert sorted(results)==[('error',409),('ok',2)]
    with Session(engine) as db:
        assert db.query(CostingSignoff).filter_by(target='estimate',target_id=eid,stage='production').count()==1
        assert db.query(CostingRevision).filter_by(estimate_id=eid).count()==2
        assert verify(db)['state']=='consistent'
