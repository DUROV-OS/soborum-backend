"""Автономный агент (0112): рабочее пространство, плейбук, уровни, ограничители."""

from datetime import datetime, timedelta, timezone

import pytest

from app.agents.autonomy import playbook, runner
from app.agents.autonomy import workspace as ws
from app.agents.autonomy.guardrails import GuardrailViolation, assert_allowed, resolve_level
from app.agents.autonomy.models import AgentActionCounter, AgentAutoRun, AgentPriority, AgentWatch
from app.agents.autonomy.playbook import Finding
from app.clients.models import Client, ClientStage
from app.common.module_access import Module
from app.cycle.models import Cycle
from app.notifications.models import Notification, NotificationKind
from app.tasks import service as task_service
from app.tasks.models import Task, TaskLinkType, TaskStatus
from app.warehouse.models import MaterialCategory, Warehouse, WarehouseMaterial

MSK = runner.TZ


def _stalled_task(db, assignee, title="Собрать каркас", days=5):
    task = task_service.create_task(db, title=title, assignee_ids=[assignee.id])
    task.created_at = datetime.now(timezone.utc) - timedelta(days=days)
    db.commit()
    assert task.status == TaskStatus.READY
    return task


def _proposals(db):
    return db.query(Task).filter(Task.link_type == TaskLinkType.AGENT_PROPOSAL).all()


def _agent_notes(db, user):
    return db.query(Notification).filter(
        Notification.user_id == user.id, Notification.kind == NotificationKind.AGENT_INITIATIVE
    ).all()


@pytest.fixture(autouse=True)
def _no_max_digest(monkeypatch):
    """Сводка в MAX (max_digest) проверяется в test_agents_autonomy_outbox.py; здесь мешает счёту карточек."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "agent_max_chat_id", 0)


def test_first_run_creates_priorities_and_journal(db, make_user):
    make_user(admin=True)
    run = runner.run_once(db, trigger="manual")
    assert set(ws.SECTIONS) == {p.section for p in db.query(AgentPriority)}
    assert run.finished_at is not None and run.trigger == "manual"
    db.expire_all()
    saved = db.get(AgentAutoRun, run.id)
    assert saved.sections == ws.ordered_sections(db) and set(saved.sections) == set(ws.SECTIONS)
    assert db.query(AgentAutoRun).count() == 1


def test_stalled_task_level_a_notifies_once_and_counts(db, make_user):
    worker = make_user(Module.TASKS)
    make_user(admin=True)
    _stalled_task(db, worker)
    first = runner.run_once(db)
    assert [a["level"] for a in first.actions if a["scenario"] == "task_stalled"] == ["A"]
    notes = _agent_notes(db, worker)
    assert len(notes) == 1 and notes[0].title.startswith("Инициатива ИИ")
    assert db.get(AgentActionCounter, "task_stalled").repeats == 1
    # второй прогон сразу: то же условие, но напоминать раньше суток нельзя
    second = runner.run_once(db)
    assert not [a for a in second.actions if a["scenario"] == "task_stalled"]
    assert len(_agent_notes(db, worker)) == 1
    watch = db.query(AgentWatch).filter(AgentWatch.scenario == "task_stalled").one()
    assert watch.seen_count == 2


def test_overloaded_assignee_gets_level_b_card_without_duplicates(db, make_user):
    worker = make_user(Module.TASKS)
    admin = make_user(admin=True)
    for i in range(playbook.OVERLOAD_OPEN_TASKS + 1):
        task_service.create_task(db, title=f"Занят {i}", assignee_ids=[worker.id])
    db.commit()
    _stalled_task(db, worker, title="Старая")
    run = runner.run_once(db)
    cards = _proposals(db)
    stalled = [c for c in cards if (c.link_meta or {}).get("scenario") == "task_stalled"]
    assert stalled and all(c.status == TaskStatus.IN_REVIEW for c in stalled)
    assert all(admin in c.assignees for c in stalled)
    assert "Инициатива ИИ" in stalled[0].description
    assert any(a["level"] == "B" for a in run.actions)
    count = len(cards)
    runner.run_once(db)
    assert len(_proposals(db)) == count


def test_repeat_limit_moves_level_a_to_b(db):
    finding = Finding(scenario="task_stalled", key="k", section="tasks", level="A", title="t", text="x")
    assert resolve_level(db, finding)[0] == "A"
    counter = ws.get_counter(db, "task_stalled")
    counter.repeats = ws.REPEAT_LIMIT
    level, reason = resolve_level(db, finding)
    assert level == "B" and "без реакции" in reason


def test_external_and_uncertain_are_always_b(db):
    external = Finding(scenario="s", key="k", section="clients", level="A", title="t", text="x", external=True)
    uncertain = Finding(scenario="s", key="k", section="clients", level="A", title="t", text="x", uncertain=True)
    assert resolve_level(db, external)[0] == "B"
    assert resolve_level(db, uncertain)[0] == "B"


def test_guardrails_forbid_money_access_delete_external():
    for kind in ("money", "access_change", "delete", "external_send"):
        with pytest.raises(GuardrailViolation):
            assert_allowed(kind)
    with pytest.raises(GuardrailViolation):
        assert_allowed("notify", Module.ACCOUNTING)
    assert_allowed("notify", Module.TASKS)


def test_completed_client_is_not_messaged_externally(db, make_user):
    admin = make_user(admin=True)
    cycle = Cycle()
    db.add(cycle)
    db.flush()
    db.add(Client(cycle_id=cycle.id, stage=ClientStage.COMPLETED, full_name="Иван Завершённый", phone="+79990000000", email="done@example.com"))
    db.commit()
    run = runner.run_once(db)
    acts = [a for a in run.actions if a["scenario"] == "client_completed"]
    assert [a["level"] for a in acts] == ["B"]
    assert "внешней отправки" in acts[0]["reason"]
    assert any(c.assignees == [admin] for c in _proposals(db))


def test_no_supplies_notifies_warehouse_and_admin(db, make_user):
    storekeeper = make_user(Module.WAREHOUSE)
    admin = make_user(admin=True)
    db.add(WarehouseMaterial(warehouse=Warehouse.TECHNOLOGY, category=MaterialCategory.NONE, title="Брус",
                             code="B1", unit="шт", quantity_in_stock=5, threshold=1))
    db.commit()
    runner.run_once(db)
    assert len(_agent_notes(db, storekeeper)) == 1
    assert len(_agent_notes(db, admin)) == 1


def test_watch_resolves_when_condition_clears(db, make_user):
    worker = make_user(Module.TASKS)
    task = _stalled_task(db, worker)
    runner.run_once(db)
    assert db.query(AgentWatch).filter_by(status="open").count() == 1
    task.status = TaskStatus.IN_PROGRESS
    db.commit()
    run = runner.run_once(db)
    assert db.query(AgentWatch).filter_by(status="resolved").count() == 1
    assert "Закрыто наблюдений" in run.summary


def test_escalation_after_deadline_creates_card(db, make_user):
    worker = make_user(Module.TASKS)
    make_user(admin=True)
    _stalled_task(db, worker)
    runner.run_once(db)
    watch = db.query(AgentWatch).one()
    watch.escalate_after = datetime.now(timezone.utc) - timedelta(hours=1)
    db.commit()
    run = runner.run_once(db)
    assert [a["level"] for a in run.actions if a["scenario"] == "task_stalled"] == ["B"]
    db.refresh(watch)
    assert watch.status == "escalated"


def test_priority_rises_on_repeated_signal_and_falls_in_quiet(db, make_user):
    worker = make_user(Module.TASKS)
    task = _stalled_task(db, worker)
    runner.run_once(db)
    assert db.get(AgentPriority, "tasks").weight == ws.INITIAL_WEIGHT  # первый сигнал не меняет
    runner.run_once(db)
    assert db.get(AgentPriority, "tasks").weight == ws.INITIAL_WEIGHT + 1
    task.status = TaskStatus.DONE
    db.commit()
    for _ in range(3):
        runner.run_once(db)
    assert db.get(AgentPriority, "tasks").weight == ws.INITIAL_WEIGHT  # тишина три раза — минус один
    first = runner.run_once(db).sections[0]
    assert first != "tasks" or db.get(AgentPriority, "tasks").weight >= max(
        p.weight for p in db.query(AgentPriority)
    )


def test_reading_notification_resets_repeat_counter(db, make_user):
    worker = make_user(Module.TASKS)
    _stalled_task(db, worker)
    runner.run_once(db)
    assert db.get(AgentActionCounter, "task_stalled").repeats == 1
    note = _agent_notes(db, worker)[0]
    note.read_at = datetime.now(timezone.utc) + timedelta(seconds=1)
    db.commit()
    runner.run_once(db)
    counter = db.get(AgentActionCounter, "task_stalled")
    assert counter.repeats == 0 and counter.last_reaction_at is not None


@pytest.mark.parametrize(
    "when,expected",
    [
        (datetime(2026, 10, 14, 3, 0, tzinfo=MSK), False),   # ночь
        (datetime(2026, 10, 18, 12, 0, tzinfo=MSK), False),  # воскресенье
        (datetime(2026, 10, 14, 12, 0, tzinfo=MSK), True),   # рабочий день, первый запуск
    ],
)
def test_schedule_window(db, when, expected):
    assert runner.should_run(db, when)[0] is expected


def test_schedule_gaps_and_activity(db, make_user):
    worker = make_user(Module.TASKS)
    # Времена в UTC: SQLite не хранит пояс, а код читает наивные значения как UTC.
    now = datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc)  # 12:00 МСК
    db.add(AgentAutoRun(started_at=now - timedelta(minutes=30), finished_at=now, sections=[], deviations=[],
                        actions=[], signals={}))
    db.commit()
    assert runner.should_run(db, now) == (False, "слишком рано")
    db.query(AgentAutoRun).update({"started_at": now - timedelta(hours=2)})
    db.commit()
    assert runner.should_run(db, now) == (False, "нет активности")
    task = task_service.create_task(db, title="x", assignee_ids=[worker.id])
    task_service.timelog.record_stage(db, task, TaskStatus.READY, TaskStatus.IN_PROGRESS, actor=worker)
    db.commit()
    ok, why = runner.should_run(db, now)
    assert ok and "активность" in why
    db.query(AgentAutoRun).update({"started_at": now - timedelta(hours=5)})
    db.commit()
    assert runner.should_run(db, now) == (True, "плановый прогон")


def test_api_admin_only_and_manual_run(db, make_user, api):
    worker = make_user(Module.TASKS)
    admin = make_user(admin=True)
    denied = api(worker).get("/api/agents/autonomy/state")
    assert denied.status_code in (401, 403)
    client = api(admin)
    run = client.post("/api/agents/autonomy/run")
    assert run.status_code == 200 and run.json()["trigger"] == "manual"
    state = client.get("/api/agents/autonomy/state")
    body = state.json()
    assert len(body["priorities"]) == len(ws.SECTIONS)
    assert len(body["runs"]) == 1 and "next_run_hint" in body


def test_resolve_watch_by_admin_resets_counter(db, make_user, api):
    worker = make_user(Module.TASKS)
    admin = make_user(admin=True)
    _stalled_task(db, worker)
    runner.run_once(db)
    watch = db.query(AgentWatch).one()
    resp = api(admin).post(f"/api/agents/autonomy/watchlist/{watch.id}/resolve")
    assert resp.status_code == 200 and resp.json()["status"] == "resolved"
    assert db.get(AgentActionCounter, "task_stalled").repeats == 0
    assert api(admin).post("/api/agents/autonomy/watchlist/9999/resolve").status_code == 404


def test_pulse_attention_queue_shows_agent_proposals(db, make_user):
    from app.dashboard.overview import generate_today

    worker = make_user(Module.TASKS)
    admin = make_user(admin=True)
    for i in range(playbook.OVERLOAD_OPEN_TASKS + 1):
        task_service.create_task(db, title=f"Занят {i}", assignee_ids=[worker.id])
    db.commit()
    _stalled_task(db, worker, title="Старая")
    assert not [a for a in generate_today(db, admin).actions if a.id == "agent:proposals"]
    runner.run_once(db)
    row = [a for a in generate_today(db, admin).actions if a.id == "agent:proposals"]
    assert row and row[0].count >= 1 and row[0].title.startswith("Инициатива ИИ")
    # исполнитель карточки не назначен -> строки у него нет
    assert not [a for a in generate_today(db, worker).actions if a.id == "agent:proposals"]


def test_section_signal_carries_agent_note(db, make_user):
    from app.dashboard.overview import generate_section_signal

    worker = make_user(Module.TASKS)
    admin = make_user(admin=True)
    _stalled_task(db, worker)
    assert generate_section_signal(db, admin, "tasks").agent_note is None
    runner.run_once(db)
    note = generate_section_signal(db, admin, "tasks").agent_note
    assert note and note.startswith("Инициатива ИИ за сутки") and "Собрать каркас" in note


def test_worsening_from_a_to_b_creates_card(db, make_user):
    worker = make_user(Module.TASKS)
    make_user(admin=True)
    _stalled_task(db, worker)
    runner.run_once(db)
    assert not _proposals(db)
    for i in range(playbook.OVERLOAD_OPEN_TASKS + 1):
        task_service.create_task(db, title=f"Занят {i}", assignee_ids=[worker.id])
    db.commit()
    run = runner.run_once(db)
    assert [a["level"] for a in run.actions if a["scenario"] == "task_stalled"] == ["B"]
    assert len(_proposals(db)) == 1
    runner.run_once(db)
    assert len(_proposals(db)) == 1


def test_actions_land_in_marina_activity_feed(db, make_user):
    from app.ai.models import AgentActivity

    worker = make_user(Module.TASKS)
    admin = make_user(admin=True)
    _stalled_task(db, assignee=worker, title="Собрать каркас", days=5)
    db.commit()

    runner.run_once(db)

    rows = db.query(AgentActivity).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.autonomous is True
    assert row.title.startswith("Инициатива ИИ")
    assert row.related_section == "tasks"

    runner.run_once(db)
    rows = db.query(AgentActivity).all()
    assert len(rows) == 1

    for i in range(playbook.OVERLOAD_OPEN_TASKS + 1):
        task_service.create_task(db, title=f"Занят {i}", assignee_ids=[worker.id])
    db.commit()

    runner.run_once(db)

    rows = db.query(AgentActivity).order_by(AgentActivity.id).all()
    assert [r.autonomous for r in rows] == [True, False]


def test_repeat_counter_counts_once_per_run_not_per_finding(db, make_user):
    make_user(admin=True)
    for i in range(playbook.MAX_PER_SCENARIO):
        _stalled_task(db, make_user(Module.TASKS), title=f"Зависла {i}")
    run = runner.run_once(db)
    levels = [a["level"] for a in run.actions if a["scenario"] == "task_stalled"]
    assert levels == ["A"] * playbook.MAX_PER_SCENARIO
    assert db.get(AgentActionCounter, "task_stalled").repeats == 1


def test_new_cards_per_scenario_are_capped_and_resume_next_run(db, make_user):
    worker = make_user(Module.TASKS)
    make_user(admin=True)
    for i in range(playbook.OVERLOAD_OPEN_TASKS + 1):
        task_service.create_task(db, title=f"Занят {i}", assignee_ids=[worker.id])
    for i in range(8):
        _stalled_task(db, worker, title=f"Старая {i}")
    run = runner.run_once(db)
    stalled = [c for c in _proposals(db) if (c.link_meta or {}).get("scenario") == "task_stalled"]
    assert len(stalled) == runner.MAX_NEW_CARDS_PER_SCENARIO
    assert "Отложено" in run.summary
    runner.run_once(db)
    stalled = [c for c in _proposals(db) if (c.link_meta or {}).get("scenario") == "task_stalled"]
    assert len(stalled) == min(runner.MAX_NEW_CARDS_PER_SCENARIO * 2, 8)


def test_open_cards_never_exceed_global_cap(db, make_user, monkeypatch):
    monkeypatch.setattr(runner, "MAX_OPEN_PROPOSALS", 3)
    monkeypatch.setattr(runner, "MAX_NEW_CARDS_PER_SCENARIO", 10)
    worker = make_user(Module.TASKS)
    make_user(admin=True)
    for i in range(playbook.OVERLOAD_OPEN_TASKS + 1):
        task_service.create_task(db, title=f"Занят {i}", assignee_ids=[worker.id])
    for i in range(8):
        _stalled_task(db, worker, title=f"Старая {i}")
    run = runner.run_once(db)
    assert len([c for c in _proposals(db) if c.status == TaskStatus.IN_REVIEW]) == 3
    assert "Отложено" in run.summary
    runner.run_once(db)  # лимит держится и на следующем прогоне, пока карточки не решены
    assert len([c for c in _proposals(db) if c.status == TaskStatus.IN_REVIEW]) == 3
    task_service.close_with_resolution(db, _proposals(db)[0], make_user(admin=True), comment="решено")
    db.commit()
    runner.run_once(db)  # решена одна — освободилось место ровно под одну новую
    assert len([c for c in _proposals(db) if c.status == TaskStatus.IN_REVIEW]) == 3


def test_close_proposals_endpoint_closes_without_deleting(db, make_user, api):
    worker = make_user(Module.TASKS)
    admin = make_user(admin=True)
    for i in range(playbook.OVERLOAD_OPEN_TASKS + 1):
        task_service.create_task(db, title=f"Занят {i}", assignee_ids=[worker.id])
    db.commit()
    
    _stalled_task(db, worker, title="Старая")
    runner.run_once(db)
    
    proposals = _proposals(db)
    assert len(proposals) >= 1
    assert all(p.status == TaskStatus.IN_REVIEW for p in proposals)
    
    resp_non_admin = api(worker).post("/api/agents/autonomy/proposals/close")
    assert resp_non_admin.status_code in (401, 403)
    
    resp = api(admin).post("/api/agents/autonomy/proposals/close")
    assert resp.status_code == 200
    assert resp.json()["closed"] == len(proposals)
    
    db.expire_all()
    proposals_after = _proposals(db)
    assert len(proposals_after) == len(proposals)
    assert all(p.status == TaskStatus.DONE for p in proposals_after)
    
    resp_repeat = api(admin).post("/api/agents/autonomy/proposals/close")
    assert resp_repeat.status_code == 200
    assert resp_repeat.json()["closed"] == 0
    
    assert ws.get_counter(db, "task_stalled").repeats == 0
