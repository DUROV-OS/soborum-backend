"""Предложения по развитию бизнеса — подраздел «Марина» → «Развитие» (0036-a).

Демо-сид (идемпотентность, prod-guard), GET /api/ai/growth-proposals,
POST /api/ai/growth-proposals/{id}/prepare-task и его отказы.
"""

from app.ai.demo_seed import ensure_growth_proposals_seed
from app.ai.models import GrowthProposal, GrowthProposalStatus
from app.common.module_access import Module
from app.core.config import settings
from app.tasks.models import Task, TaskLinkType


def test_seed_creates_rows(db):
    created = ensure_growth_proposals_seed(db)
    assert created == 4
    assert db.query(GrowthProposal).count() == 4
    assert all(row.status == GrowthProposalStatus.OPEN for row in db.query(GrowthProposal).all())


def test_seed_is_idempotent(db):
    first = ensure_growth_proposals_seed(db)
    assert first > 0
    assert ensure_growth_proposals_seed(db) == 0
    assert db.query(GrowthProposal).count() == first


def test_seed_skips_if_rows_already_exist(db):
    db.add(GrowthProposal(
        title="Своё предложение", problem="…", checkable_result="…",
        executor_and_estimate="…", expected_effect="…",
    ))
    db.commit()

    assert ensure_growth_proposals_seed(db) == 0
    assert db.query(GrowthProposal).count() == 1


def test_seed_does_not_run_in_prod(db, monkeypatch):
    monkeypatch.setattr(settings, "app_env", "prod")
    assert ensure_growth_proposals_seed(db) == 0
    assert db.query(GrowthProposal).count() == 0


def test_endpoint_returns_seeded_proposals(api, make_user, db):
    ensure_growth_proposals_seed(db)
    client = api(make_user(Module.AI))

    response = client.get("/api/ai/growth-proposals")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 4
    assert {
        "id", "title", "problem", "checkable_result", "executor_and_estimate",
        "expected_effect", "status", "task_id", "created_at",
    } <= body[0].keys()
    assert body[0]["status"] == "open"


def test_endpoint_requires_ai_module(api, make_user, db):
    ensure_growth_proposals_seed(db)
    client = api(make_user())  # без Module.AI

    assert client.get("/api/ai/growth-proposals").status_code == 403


def test_prepare_task_creates_real_task(api, make_user, db):
    ensure_growth_proposals_seed(db)
    proposal = db.query(GrowthProposal).order_by(GrowthProposal.id).first()
    client = api(make_user(Module.AI, Module.TASKS))

    response = client.post(f"/api/ai/growth-proposals/{proposal.id}/prepare-task")
    assert response.status_code == 200
    body = response.json()
    assert body["proposal"]["status"] == "task_created"
    assert body["task"]["title"] == proposal.title
    assert body["proposal"]["task_id"] == body["task"]["id"]

    db.refresh(proposal)
    assert proposal.status == GrowthProposalStatus.TASK_CREATED
    assert proposal.task_id is not None

    task = db.get(Task, proposal.task_id)
    assert task is not None
    assert task.link_type == TaskLinkType.GROWTH_PROPOSAL
    assert task.link_id == proposal.id

    tasks_client = api(make_user(Module.TASKS))
    listed = tasks_client.get("/api/tasks/")
    assert listed.status_code == 200
    assert any(t["id"] == task.id for t in listed.json())


def test_prepare_task_twice_returns_409(api, make_user, db):
    ensure_growth_proposals_seed(db)
    proposal = db.query(GrowthProposal).order_by(GrowthProposal.id).first()
    client = api(make_user(Module.AI, Module.TASKS))

    first = client.post(f"/api/ai/growth-proposals/{proposal.id}/prepare-task")
    assert first.status_code == 200

    second = client.post(f"/api/ai/growth-proposals/{proposal.id}/prepare-task")
    assert second.status_code == 409


def test_prepare_task_missing_proposal_returns_404(api, make_user, db):
    client = api(make_user(Module.AI, Module.TASKS))
    assert client.post("/api/ai/growth-proposals/999/prepare-task").status_code == 404
