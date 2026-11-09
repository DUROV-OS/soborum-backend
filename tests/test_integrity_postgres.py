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
        for table in ('actual_cost_closures','actual_cost_decisions','actual_cost_versions','actual_cost_records','actual_cost_documents','payment_evidence_checks','payment_evidence_versions','payment_evidence_documents','costing_signoffs','costing_evidence_documents','costing_overhead_plans','costing_approval_policy'):
            conn.execute(text('DROP TABLE '+table))
        covered = set()
        for name in ('oc0911202601_integrity_ledger.py','ca0911202601_costing_approvals.py','pe0911202601_payment_evidence.py','ac0911202601_actual_costs.py','jo0911202601_order_journey.py'):
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


def test_payment_evidence_concurrency_and_immutable_history(engine):
    from fastapi import HTTPException
    from app.accounting import evidence as e
    from app.accounting.models import MoneyMovement,MoneyDirection,MoneySubkind,MoneyMovementStatus
    from app.accounting.evidence_models import PaymentEvidenceDocument,PaymentEvidenceVersion,PaymentEvidenceCheck
    from app.accounting.evidence_schemas import EvidenceInput,CheckInput
    from app.common.files import save_bytes_file,FilePurpose
    from app.costing.governance_models import CostingApprovalPolicy
    from app.costing.service import lock
    with Session(engine) as db:
        user=new_user(db);uid=user.id
        db.get(CostingApprovalPolicy,1).accounting_user_id=uid
        row=MoneyMovement(direction=MoneyDirection.EXPENSE,subkind=MoneySubkind.OTHER_EXPENSE,amount=100.01,tax=0,initiator_id=uid,status=MoneyMovementStatus.POSTED)
        db.add(row);db.flush();rid=row.id
        asset=save_bytes_file(db,'synthetic.txt','text/plain',b'synthetic documented amount 100.01',FilePurpose.MONEY_MOVEMENT_DOCUMENT,user)
        doc=PaymentEvidenceDocument(movement_id=rid,file_id=asset.id,actor_id=uid);db.add(doc);db.commit();docid=doc.id
        proof={'file_id':asset.id,'locator':'Synthetic first line'}
        payload={'expected_version':0,'source_hash':e.canonical(e.source(row)),'nature':'expense','payment_evidence':proof,
                 'allocations':[{'amount':'100.01','category':'factory','scope':'month','month':'2026-11-01','evidence':proof}], 'reason':'Synthetic evidence concurrency'}
    barrier=Barrier(2)
    def worker(_):
        with Session(engine) as db:
            actor=db.get(User,uid);bind_actor(db,actor);barrier.wait(timeout=10)
            try:
                payment=e.movement(db,rid,True)
                result=e.save_version(db,payment,EvidenceInput(**payload,request_id=uuid4()),actor)
                value=result.version;db.commit();return value
            except HTTPException as error:
                db.rollback();return error.status_code
    with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(worker,range(2)))
    assert sorted(results)==[1,409]
    with Session(engine) as db:
        version=db.query(PaymentEvidenceVersion).filter_by(movement_id=rid).one()
        lock(db);actor=db.get(User,uid);row=e.movement(db,rid,True)
        checked=e.sign(db,row,CheckInput(request_id=uuid4(),version=1,confirmed=True,reason='Personally verified test documents'),actor);db.commit()
        for table,id in [('payment_evidence_documents',docid),('payment_evidence_versions',version.id),('payment_evidence_checks',checked.id)]:
            for statement in (f'UPDATE {table} SET id=id WHERE id=:id',f'DELETE FROM {table} WHERE id=:id'):
                with pytest.raises(DBAPIError): db.execute(text(statement),{'id':id})
                db.rollback()
        db.execute(text('ALTER TABLE payment_evidence_versions DISABLE TRIGGER payment_evidence_immutable'))
        assert protection(db)['state']=='incomplete';db.rollback()
        assert protection(db)['state']=='active' and verify(db)['state']=='consistent'


def test_two_orders_cannot_allocate_the_same_purchase_capacity(engine, monkeypatch):
    from datetime import datetime, timezone
    from fastapi import HTTPException
    from app.accounting.models import SupplierOrder, Counterparty, Organization
    from app.accounting.seed import ensure_organizations_seed
    from app.costing import governance
    from app.costing.integration import fingerprint
    from app.costing.models import CostingEstimate, CostingRevision, CostingOrderLink
    from app.sales.models import SalesOrder, SalesEvent
    from app.sales.journey_supply import allocate, Allocation
    from app.warehouse.models import Supplier, WarehouseMaterial, Warehouse, MaterialCategory
    # Approval workflow is exercised with actual documents in test_order_journey;
    # here isolate the cross-order PostgreSQL capacity race.
    monkeypatch.setattr(governance, 'assert_approved', lambda *args: None)
    with Session(engine) as db:
        ensure_organizations_seed(db); actor = new_user(db); uid = actor.id
        cp = Counterparty(name='Synthetic capacity buyer', is_active=True)
        supplier = Supplier(name='Synthetic capacity supplier')
        material = WarehouseMaterial(title='Capacity product', code='RACE-JOURNEY', warehouse=Warehouse.TECHNOLOGY,
            category=MaterialCategory.NONE, unit='шт', is_fractional=False, quantity_in_stock=0, purchase_price=0, threshold=0)
        db.add_all([cp, supplier, material]); db.flush()
        purchase = SupplierOrder(supplier_id=supplier.id, items=[{'warehouse_material_id': material.id, 'material': material.title,
            'quantity': 2, 'unit_price': 1, 'unit': 'шт', 'stock_tracked': True}], total_cost=2)
        db.add(purchase); db.flush(); pid = purchase.id
        ids = []
        for _ in range(2):
            order = SalesOrder(counterparty_id=cp.id, organization_id=db.query(Organization).first().id, status='confirmed',
                actor_id=uid, occurred_at=datetime.now(timezone.utc), total=10, lines=[{'material_id': material.id, 'title': material.title,
                'stock_tracked': True, 'unit': 'шт', 'quantity': '2', 'unit_price': '5', 'line_total': '10', 'opening_shipped': '0'}])
            quote = CostingEstimate(actor_id=uid, title='Synthetic capacity quote', status='approved')
            db.add_all([order, quote]); db.flush()
            revision = CostingRevision(estimate_id=quote.id, number=1, actor_id=uid, reason='Synthetic', request_id=str(uuid4()), request_data={}, snapshot={})
            db.add(revision); db.flush()
            db.add(CostingOrderLink(order_id=order.id, line_index=0, estimate_id=quote.id, revision_id=revision.id, fingerprint=fingerprint(order,0), actor_id=uid))
            ids.append(order.id)
        db.commit()
    barrier = Barrier(2)
    def worker(oid):
        with Session(engine) as db:
            actor = db.get(User, uid); bind_actor(db, actor)
            body = Allocation(request_id=uuid4(), version=1, order_line=0, supplier_order_id=pid, supplier_version=1, supplier_line=0, quantity='2')
            barrier.wait(timeout=10)
            try: allocate(oid, body, db, actor); return 200
            except HTTPException as error: db.rollback(); return error.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(worker, ids)) == [200, 409]
    with Session(engine) as db:
        row = db.query(SalesEvent).filter_by(kind='supply_allocated').one()
        for statement in ('UPDATE sales_events SET data=data WHERE id=:id', 'DELETE FROM sales_events WHERE id=:id'):
            with pytest.raises(DBAPIError): db.execute(text(statement), {'id': row.id})
            db.rollback()
        db.execute(text('ALTER TABLE sales_events DISABLE TRIGGER journey_events_immutable'))
        assert protection(db)['state'] == 'incomplete'; db.rollback()
        assert verify(db)['state'] == 'consistent'


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


def test_actual_cost_concurrency_and_immutable_decisions(engine):
    from fastapi import HTTPException
    from app.accounting import actual as a
    from app.accounting.actual_schemas import CostInput,CostDecision
    from app.accounting.actual_models import ActualCostDocument,ActualCostRecord,ActualCostVersion,ActualCostClosure
    from app.accounting.models import Organization
    from app.accounting.seed import ensure_organizations_seed
    from app.common.files import save_bytes_file,FilePurpose
    from app.costing.models import CostingEstimate
    from app.costing.governance_models import CostingApprovalPolicy
    from app.costing.service import lock
    with Session(engine) as db:
        ensure_organizations_seed(db);actor=new_user(db);uid=actor.id
        db.get(CostingApprovalPolicy,1).accounting_user_id=uid
        asset=save_bytes_file(db,'synthetic-actual.txt','text/plain',b'isolated incurred workshop expense',FilePurpose.COSTING_EVIDENCE,actor)
        document=ActualCostDocument(file_id=asset.id,actor_id=uid);db.add(document);db.commit();docid=document.id
        payload={'kind':'document','organization_id':db.query(Organization).first().id,'issuer':'Test landlord','number':'actual-'+str(uuid4()),'document_date':'2026-11-01','incurred_on':'2026-11-01','line_ref':'1',
            'evidence':{'file_id':asset.id,'locator':'Test line 1'},'gross_amount':'100.01','excluded_tax':'0','tax_basis':'No excluded tax, test proof',
            'allocations':[{'category':'factory','amount':'100.01','month':'2026-11-01','locator':'Test line 1'}],'reason':'Synthetic cost concurrency'}
    barrier=Barrier(2)
    def worker(_):
        with Session(engine) as db:
            actor=db.get(User,uid);bind_actor(db,actor);barrier.wait(timeout=10)
            try:
                lock(db);v=a.save(db,CostInput(**payload,request_id=uuid4()),actor);db.commit();return v.version
            except HTTPException as error:db.rollback();return error.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(worker,range(2)))
    assert sorted(results)==[1,409]
    with Session(engine) as db:
        version=db.query(ActualCostVersion).one();actor=db.get(User,uid);lock(db)
        decision=a.decide(db,version.record_id,CostDecision(request_id=uuid4(),version=1,source_hash=a.canonical(version.content),action='recognize',confirmed=True,reason='Personally checked synthetic cost'),actor)
        project=CostingEstimate(title='Synthetic closure protection',actor_id=uid);db.add(project);db.flush()
        closure=ActualCostClosure(project_id=project.id,stage='accounting',source_hash='0'*64,actor_id=uid,request_id=str(uuid4()),content={'synthetic':True});db.add(closure);db.commit()
        for table,id in [('actual_cost_documents',docid),('actual_cost_records',version.record_id),('actual_cost_versions',version.id),('actual_cost_decisions',decision.id),('actual_cost_closures',closure.id)]:
            for statement in (f'UPDATE {table} SET id=id WHERE id=:id',f'DELETE FROM {table} WHERE id=:id'):
                with pytest.raises(DBAPIError):db.execute(text(statement),{'id':id})
                db.rollback()
        db.execute(text('ALTER TABLE actual_cost_closures DISABLE TRIGGER actual_cost_immutable'))
        assert protection(db)['state']=='incomplete';db.rollback()
        assert protection(db)['state']=='active' and verify(db)['state']=='consistent'
