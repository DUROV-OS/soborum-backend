"""/ready (0084-a): 200 только когда БД отвечает и ревизия alembic в ней
совпадает с головой скриптов; иначе 503 с кодом причины и без деталей
подключения. /health остаётся безусловным liveness."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core import readiness
from app.db.session import get_db
from app.main import app


@pytest.fixture
def probe(db):
    def client_with(session):
        app.dependency_overrides[get_db] = lambda: session
        return TestClient(app)
    yield client_with
    app.dependency_overrides.clear()


def _stamp(db, *revisions):
    db.execute(text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL)"))
    db.execute(text("DELETE FROM alembic_version"))
    for rev in revisions:
        db.execute(text("INSERT INTO alembic_version (version_num) VALUES (:rev)"), {"rev": rev})
    db.commit()


def test_head_from_scripts_is_single():
    assert len(readiness.expected_heads()) == 1


def test_ready_when_db_is_at_head(db, probe):
    _stamp(db, *readiness.expected_heads())
    resp = probe(db).get("/ready")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready"}


def test_api_prefixed_alias_answers_the_same(db, probe):
    _stamp(db, *readiness.expected_heads())
    assert probe(db).get("/api/ready").json() == {"status": "ready"}


def test_503_when_revision_differs_from_code(db, probe):
    _stamp(db, "0000deadbeef")
    resp = probe(db).get("/ready")
    assert resp.status_code == 503
    assert resp.json() == {"status": "not_ready", "reason": "migration_mismatch"}


def test_503_when_migrations_never_ran(db, probe):
    resp = probe(db).get("/ready")
    assert resp.status_code == 503
    assert resp.json()["reason"] == "migration_mismatch"


class _BrokenSession:
    def execute(self, *args, **kwargs):
        raise ConnectionError("could not connect to server: postgresql://soborbum:secret@db:5432")


def test_503_when_db_unreachable_without_leaking_connection_details(probe):
    resp = probe(_BrokenSession()).get("/ready")
    assert resp.status_code == 503
    assert resp.json() == {"status": "not_ready", "reason": "db_unreachable"}
    assert "secret" not in resp.text and "postgresql" not in resp.text


def test_health_stays_liveness_only(probe):
    resp = probe(_BrokenSession()).get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
