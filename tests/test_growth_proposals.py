"""Предложения по развитию бизнеса — подраздел «Марина» → «Развитие» (0036-a,
0050-a).

Демо-сид (идемпотентность, prod-guard), GET /api/ai/growth-proposals,
POST /api/ai/growth-proposals/{id}/prepare-task и его отказы, а также
реальная генерация через GET /api/ai/growth-proposals?reload=true (0050-a).
"""

from types import SimpleNamespace

from app.ai import growth_ideation
from app.ai.demo_seed import ensure_growth_proposals_seed
from app.ai.models import GrowthProposal, GrowthProposalStatus
from app.common.module_access import Module
from app.core.config import settings
from app.tasks.models import Task, TaskLinkType

_FAKE_PROPOSALS = [
    {
        "title": "Сменить поставщика утеплителя на позицию X",
        "problem": "По позиции X один поставщик держит цену выше рынка уже несколько месяцев.",
        "checkable_result": "Заключён договор со вторым поставщиком, цена по позиции X снижена.",
        "executor_and_estimate": "Снабженец, 2 недели",
        "expected_effect": "Снижение себестоимости по позиции X",
    },
    {
        "title": "Точечная скидка постоянному клиенту",
        "problem": "Клиент с историей повторных заказов рассматривает конкурента по цене.",
        "checkable_result": "Клиент подтвердил новый заказ после согласования скидки",
        "executor_and_estimate": "Менеджер продаж, 1 неделя",
        "expected_effect": "Удержание клиента, повторная выручка",
    },
    {
        "title": "Единый шаблон причины задержки монтажа",
        "problem": "Причины задержек монтажа фиксируются в свободной форме, сложно анализировать",
        "checkable_result": "Шаблон внедрён, новые задержки фиксируются по нему",
        "executor_and_estimate": "Начальник производства, 1 неделя",
        "expected_effect": "Быстрее находить системные причины задержек",
    },
]


class _FakeMessages:
    def __init__(self, tool_input):
        self._tool_input = tool_input

    def create(self, **kwargs):
        if self._tool_input is None:
            return SimpleNamespace(content=[])
        return SimpleNamespace(
            content=[SimpleNamespace(type="tool_use", input=self._tool_input)]
        )


class _FakeClient:
    def __init__(self, tool_input):
        self.messages = _FakeMessages(tool_input)


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


def test_reload_generates_and_replaces_open_proposals(api, make_user, db, monkeypatch):
    ensure_growth_proposals_seed(db)
    monkeypatch.setattr(growth_ideation, "_get_client", lambda: _FakeClient({"proposals": _FAKE_PROPOSALS}))
    client = api(make_user(Module.AI))

    response = client.get("/api/ai/growth-proposals?reload=true")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 3
    titles = {row["title"] for row in body}
    assert titles == {p["title"] for p in _FAKE_PROPOSALS}
    # старые демо-предложения (тоже status=open) заменены, не остались вперемешку
    assert db.query(GrowthProposal).count() == 3


def test_reload_keeps_task_created_proposal(api, make_user, db, monkeypatch):
    ensure_growth_proposals_seed(db)
    prepared = db.query(GrowthProposal).order_by(GrowthProposal.id).first()
    tasks_client = api(make_user(Module.AI, Module.TASKS))
    prepare_response = tasks_client.post(f"/api/ai/growth-proposals/{prepared.id}/prepare-task")
    assert prepare_response.status_code == 200
    task_id = prepare_response.json()["task"]["id"]

    monkeypatch.setattr(growth_ideation, "_get_client", lambda: _FakeClient({"proposals": _FAKE_PROPOSALS}))
    client = api(make_user(Module.AI))
    response = client.get("/api/ai/growth-proposals?reload=true")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 4  # 3 новых + ранее подготовленное

    db.refresh(prepared)
    assert prepared.status == GrowthProposalStatus.TASK_CREATED
    assert prepared.task_id == task_id
    still_there = next(row for row in body if row["id"] == prepared.id)
    assert still_there["status"] == "task_created"
    assert still_there["task_id"] == task_id


def test_reload_without_api_key_returns_400_and_keeps_list(api, make_user, db):
    ensure_growth_proposals_seed(db)
    assert settings.anthropic_api_key == ""
    client = api(make_user(Module.AI))

    response = client.get("/api/ai/growth-proposals?reload=true")
    assert response.status_code == 400
    assert db.query(GrowthProposal).count() == 4


def test_reload_requires_ai_module(api, make_user, db):
    ensure_growth_proposals_seed(db)
    client = api(make_user())  # без Module.AI

    assert client.get("/api/ai/growth-proposals?reload=true").status_code == 403


def test_reload_bad_ai_response_returns_502_and_keeps_list(api, make_user, db, monkeypatch):
    ensure_growth_proposals_seed(db)
    monkeypatch.setattr(growth_ideation, "_get_client", lambda: _FakeClient(None))
    client = api(make_user(Module.AI))

    response = client.get("/api/ai/growth-proposals?reload=true")
    assert response.status_code == 502
    assert db.query(GrowthProposal).count() == 4


def test_reload_incomplete_proposal_returns_502_and_keeps_list(api, make_user, db, monkeypatch):
    ensure_growth_proposals_seed(db)
    broken = [dict(_FAKE_PROPOSALS[0]), _FAKE_PROPOSALS[1], _FAKE_PROPOSALS[2]]
    broken[0]["checkable_result"] = ""
    monkeypatch.setattr(growth_ideation, "_get_client", lambda: _FakeClient({"proposals": broken}))
    client = api(make_user(Module.AI))

    response = client.get("/api/ai/growth-proposals?reload=true")
    assert response.status_code == 502
    assert db.query(GrowthProposal).count() == 4
