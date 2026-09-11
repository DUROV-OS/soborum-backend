"""Демо-сидер «Действия агента» (app.ai.demo_seed) — 0033.

Идемпотентность, prod-guard, привязка к реальному клиенту, если он уже есть
в базе, и что GET /api/ai/agent-actions отдаёт созданные записи.
"""

from app.ai.demo_seed import ensure_agent_activity_seed
from app.ai.models import AgentActivity
from app.clients.models import Client
from app.common.module_access import Module
from app.core.config import settings
from app.cycle.models import Cycle


def test_seed_creates_rows(db):
    created = ensure_agent_activity_seed(db)
    assert created == 10
    assert db.query(AgentActivity).count() == 10


def test_seed_is_idempotent(db):
    first = ensure_agent_activity_seed(db)
    assert first > 0
    assert ensure_agent_activity_seed(db) == 0
    assert db.query(AgentActivity).count() == first


def test_seed_skips_if_rows_already_exist(db):
    db.add(AgentActivity(title="Своя запись", detail="…", autonomous=True))
    db.commit()

    assert ensure_agent_activity_seed(db) == 0
    assert db.query(AgentActivity).count() == 1


def test_seed_does_not_run_in_prod(db, monkeypatch):
    monkeypatch.setattr(settings, "app_env", "prod")
    assert ensure_agent_activity_seed(db) == 0
    assert db.query(AgentActivity).count() == 0


def test_seed_links_to_real_client(db):
    cycle = Cycle()
    db.add(cycle)
    db.flush()
    client = Client(
        cycle_id=cycle.id, full_name="Иванов Иван", phone="+70000000000",
        email="ivanov@example.com", contacts=[],
    )
    db.add(client)
    db.commit()

    ensure_agent_activity_seed(db)

    linked = db.query(AgentActivity).filter(AgentActivity.related_section == "clients").all()
    assert linked
    assert all(row.related_path == f"/clients/{client.id}" for row in linked)


def test_endpoint_returns_seeded_activity(api, make_user, db):
    ensure_agent_activity_seed(db)
    client = api(make_user(Module.AI))

    response = client.get("/api/ai/agent-actions")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 10
    assert {"id", "title", "detail", "autonomous", "created_at"} <= body[0].keys()


def test_endpoint_requires_ai_module(api, make_user, db):
    ensure_agent_activity_seed(db)
    client = api(make_user())  # без Module.AI

    assert client.get("/api/ai/agent-actions").status_code == 403
