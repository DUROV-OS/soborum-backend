"""Priority filtering keeps the existing task scope and status contracts."""

import pytest

from app.common.module_access import Module
from app.tasks.models import Task, TaskPriority, TaskStatus


def seed(db, worker, other):
    rows = {
        "own_low": Task(title="Низкий", priority=TaskPriority.LOW, status=TaskStatus.READY),
        "own_medium": Task(title="Средний", status=TaskStatus.READY),
        "own_high": Task(title="Высокий", priority=TaskPriority.HIGH, status=TaskStatus.IN_PROGRESS),
        "own_done": Task(title="Высокий завершённый", priority=TaskPriority.HIGH, status=TaskStatus.DONE),
        "other_high": Task(title="Чужой высокий", priority=TaskPriority.HIGH, status=TaskStatus.READY),
        "free_high": Task(title="Свободный высокий", priority=TaskPriority.HIGH, status=TaskStatus.READY),
    }
    for key, row in rows.items():
        row.assignees = [] if key == "free_high" else [other if key == "other_high" else worker]
    db.add_all(rows.values())
    db.commit()
    return rows


def test_priority_filter_preserves_scope_status_and_reset(db, make_user, api):
    worker = make_user(Module.TASKS)
    other = make_user(Module.TASKS)
    rows = seed(db, worker, other)
    client = api(worker)
    response = client.get("/api/tasks/?priority=high")
    assert response.status_code == 200
    assert {row["id"] for row in response.json()} == {rows[key].id for key in ("own_high", "own_done", "free_high")}
    assert {row["priority"] for row in response.json()} == {"high"}
    response = client.get("/api/tasks/?priority=high&status=open")
    assert {row["id"] for row in response.json()} == {rows[key].id for key in ("own_high", "free_high")}
    response = client.get(f"/api/tasks/?priority=high&status=open&assignee_id={worker.id}")
    assert [row["id"] for row in response.json()] == [rows["own_high"].id]
    response = client.get("/api/tasks/?priority=high&scope=claimable")
    assert [row["id"] for row in response.json()] == [rows["free_high"].id]
    response = client.get("/api/tasks/?priority=medium")
    assert [row["id"] for row in response.json()] == [rows["own_medium"].id]
    response = client.get("/api/tasks/?priority=low")
    assert [row["id"] for row in response.json()] == [rows["own_low"].id]
    response = client.get("/api/tasks/")
    assert {row["id"] for row in response.json()} == {row.id for key, row in rows.items() if key != "other_high"}
    assert client.get("/api/tasks/?scope=all&priority=high").status_code == 403
    assert api(make_user()).get("/api/tasks/?priority=high").status_code == 403


def test_priority_all_grant_remains_required_and_complete(db, make_user, api):
    worker = make_user(Module.TASKS, Module.TASKS_ALL)
    other = make_user(Module.TASKS)
    rows = seed(db, worker, other)
    response = api(worker).get("/api/tasks/?scope=all&priority=high")
    assert response.status_code == 200
    assert {row["id"] for row in response.json()} == {row.id for row in rows.values() if row.priority == TaskPriority.HIGH}


@pytest.mark.parametrize("priority", ["urgent", "HIGH", ""])
def test_invalid_priority_is_rejected(api, make_user, priority):
    assert api(make_user(Module.TASKS)).get(f"/api/tasks/?priority={priority}").status_code == 422
