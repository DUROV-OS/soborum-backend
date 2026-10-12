"""«Итоги дня»/«Итоги недели» на «Пульсе» (0027): агрегаты за период из дат
существующих сущностей, сравнение с предыдущим периодом, права по разделам."""

from datetime import datetime, timedelta, timezone

from app.clients import service as client_service
from app.clients.schemas import ClientCreate
from app.common.module_access import Module
from app.dashboard.period_summary import windows
from app.tasks.models import Task, TaskStageEvent, TaskStatus


def _client(db, name, created_at):
    client = client_service.create_client(
        db, ClientCreate(full_name=name, phone="+70000000000", email=f"{name}@example.com")
    )
    client.created_at = created_at
    db.commit()
    return client


def _metrics(body, section):
    sec = next(s for s in body["sections"] if s["section"] == section)
    return {m["key"]: m for m in sec["metrics"]}


def test_windows_day_and_week_have_expected_length():
    now = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
    day, prev_day = windows("day", now)
    week, prev_week = windows("week", now)

    assert day.end - day.start == timedelta(days=1)
    assert prev_day.end == day.start
    assert week.end - week.start == timedelta(days=7)
    assert prev_week.end == week.start
    assert week.end == day.end


def test_day_counts_today_and_compares_with_yesterday(api, make_user, db):
    now = datetime.now(timezone.utc)
    _client(db, "Сегодня1", now)
    _client(db, "Сегодня2", now)
    _client(db, "Вчера", now - timedelta(days=1))
    _client(db, "Давно", now - timedelta(days=30))
    user = make_user(Module.CLIENTS)

    body = api(user).get("/api/dashboard/today/summary/day").json()

    new_clients = _metrics(body, "clients")["new_clients"]
    assert new_clients["value"] == 2
    assert new_clients["previous"] == 1


def test_week_includes_six_days_back_but_not_older(api, make_user, db):
    now = datetime.now(timezone.utc)
    _client(db, "Три дня назад", now - timedelta(days=3))
    _client(db, "Десять дней назад", now - timedelta(days=10))
    user = make_user(Module.CLIENTS)

    body = api(user).get("/api/dashboard/today/summary/week").json()

    new_clients = _metrics(body, "clients")["new_clients"]
    assert new_clients["value"] == 1
    assert new_clients["previous"] == 1


def test_tasks_closed_counts_done_transitions_in_period(api, make_user, db):
    now = datetime.now(timezone.utc)
    task = Task(title="Сделать", status=TaskStatus.DONE)
    db.add(task)
    db.commit()
    db.add_all([
        TaskStageEvent(task_id=task.id, from_status=TaskStatus.IN_REVIEW, to_status=TaskStatus.DONE, created_at=now),
        TaskStageEvent(
            task_id=task.id, from_status=TaskStatus.IN_PROGRESS, to_status=TaskStatus.IN_REVIEW, created_at=now
        ),
    ])
    db.commit()
    user = make_user(Module.TASKS)

    body = api(user).get("/api/dashboard/today/summary/day").json()

    assert _metrics(body, "tasks")["tasks_closed"]["value"] == 1


def test_overdue_counts_only_unfinished_tasks_whose_deadline_passed_in_period(api, make_user, db):
    now = datetime.now(timezone.utc)
    db.add_all([
        Task(title="Просрочена", status=TaskStatus.IN_PROGRESS, deadline=now - timedelta(hours=1)),
        Task(title="Закрыта", status=TaskStatus.DONE, deadline=now - timedelta(hours=1)),
        Task(title="Впереди", status=TaskStatus.IN_PROGRESS, deadline=now + timedelta(days=2)),
    ])
    db.commit()
    user = make_user(Module.TASKS)

    body = api(user).get("/api/dashboard/today/summary/week").json()

    overdue = _metrics(body, "tasks")["tasks_overdue"]
    assert overdue["value"] == 1
    assert overdue["growth_is_bad"] is True


def test_sections_limited_to_user_access(api, make_user, db):
    user = make_user(Module.CLIENTS)

    for period in ("day", "week"):
        body = api(user).get(f"/api/dashboard/today/summary/{period}").json()
        assert [s["section"] for s in body["sections"]] == ["clients"]


def test_unknown_period_is_rejected(api, make_user):
    response = api(make_user(Module.CLIENTS)).get("/api/dashboard/today/summary/month")

    assert response.status_code == 422
