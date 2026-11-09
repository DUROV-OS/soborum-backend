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
        filename = Path(__file__).resolve().parents[1] / "alembic/versions/oc0911202601_integrity_ledger.py"
        spec = spec_from_file_location("integrity_migration", filename)
        migration = module_from_spec(spec); spec.loader.exec_module(migration)
        assert set(migration.TABLES) == EXPECTED_TABLES
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
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
