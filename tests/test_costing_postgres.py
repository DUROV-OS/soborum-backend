"""Opt-in checks on an already migrated, explicitly disposable PostgreSQL database."""
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.costing.models import CostingRevision
from app.costing.router import save_estimate, write_item, write_material
from app.costing.schemas import EstimateInput, ItemInput, MaterialInput
from app.users.models import User, UserRole, UserModuleAccess
from app.common.module_access import Module, AccessLevel

URL = os.getenv("COSTING_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="Requires isolated COSTING_TEST_DATABASE_URL")


@pytest.fixture
def pg():
    url = make_url(URL)
    assert url.get_backend_name() == "postgresql" and url.database == "costing_isolated_test"
    engine = create_engine(url, pool_size=5)
    with Session(engine) as db:
        user = User(email=f"costing-{uuid4()}@example.test", full_name="Тест", role=UserRole.ADMIN, hashed_password="not-a-login", is_active=True)
        db.add(user)
        db.commit()
        uid = user.id
        mat = write_material(db, user, MaterialInput(name="Тестовый материал", unit="шт", price="51", price_source="Тест", effective_on="2026-01-01"))
        expenses = {key: "0" for key in ("labor", "transport", "subcontract", "overhead", "commercial", "advertising", "taxes", "reserve")} | {"evidence": "Тест"}
        item = write_item(db, user, ItemInput(kind="template", title="Изолированный тест", content={"lines": [{"material_id": mat["id"], "quantity": "1"}], "expenses": expenses, "retail_price": "150"}))
    yield engine, uid, item
    engine.dispose()


def payload(template, **kwargs):
    return EstimateInput(request_id=uuid4(), title="Параллельный тест", template_id=template["id"], **kwargs)


def test_postgres_migrated_costing_schema_and_enum(pg):
    engine, uid, template = pg
    assert "costing_revisions" in inspect(engine).get_table_names()
    with Session(engine) as db:
        user = User(email=f"grant-{uuid4()}@example.test", full_name="Сметчик", role=UserRole.WORKER, hashed_password="not-a-login", is_active=True)
        user.module_access = [UserModuleAccess(module=Module.COSTING, level=AccessLevel.EDIT)]
        db.add(user)
        db.commit()
        db.expire_all()
        assert db.get(User, user.id).access_level(Module.COSTING) == AccessLevel.EDIT


def test_postgres_concurrent_same_request_creates_one_revision(pg):
    engine, uid, template = pg
    body = payload(template)
    barrier = Barrier(2)
    def run():
        with Session(engine) as db:
            user = db.get(User, uid)
            barrier.wait(timeout=10)
            result = save_estimate(db, user, body)
            return result["id"]
    with ThreadPoolExecutor(2) as pool:
        a, b = pool.submit(run), pool.submit(run)
        ids = [a.result(timeout=30), b.result(timeout=30)]
    assert ids[0] == ids[1]
    with Session(engine) as db:
        assert db.query(CostingRevision).filter_by(estimate_id=ids[0]).count() == 1


def test_postgres_concurrent_stale_edits_never_overwrite(pg):
    engine, uid, template = pg
    with Session(engine) as db:
        original = save_estimate(db, db.get(User, uid), payload(template))
    barrier = Barrier(2)
    def run(note):
        with Session(engine) as db:
            user = db.get(User, uid)
            body = payload(template, version=1, note=note)
            barrier.wait(timeout=10)
            try:
                return ("ok", save_estimate(db, user, body, original["id"])["version"])
            except HTTPException as exc:
                db.rollback()
                return ("error", exc.status_code)
    with ThreadPoolExecutor(2) as pool:
        a, b = pool.submit(run, "Первый"), pool.submit(run, "Второй")
        results = [a.result(timeout=30), b.result(timeout=30)]
    assert sorted(results) == [("error", 409), ("ok", 2)]
    with Session(engine) as db:
        assert db.query(CostingRevision).filter_by(estimate_id=original["id"]).count() == 2
        assert db.query(CostingRevision).filter_by(estimate_id=original["id"], number=1).one().snapshot["input"]["note"] == ""
