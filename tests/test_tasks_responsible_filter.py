"""Responsible filtering narrows the existing permission-limited task scope."""

import pytest

from app.common.module_access import Module
from app.tasks.models import Task, TaskPriority, TaskStatus


def test_responsible_filter_uses_explicit_owner_then_sole_assignee(db, make_user, api):
    worker = make_user(Module.TASKS)
    other = make_user(Module.TASKS)
    explicit = Task(title="Ответственный отдельно", responsible_id=worker.id, status=TaskStatus.READY, priority=TaskPriority.HIGH)
    explicit.assignees = [other]
    fallback = Task(title="Единственный исполнитель", status=TaskStatus.IN_PROGRESS, priority=TaskPriority.LOW)
    fallback.assignees = [worker]
    other_owner = Task(title="Я исполнитель, другой ответственный", responsible_id=other.id, status=TaskStatus.READY)
    other_owner.assignees = [worker]
    private = Task(title="Не моя задача", responsible_id=other.id, status=TaskStatus.READY)
    private.assignees = [other]
    multiple = Task(title="Два исполнителя без ответственного", status=TaskStatus.READY)
    multiple.assignees = [worker, other]
    free = Task(title="Свободная", status=TaskStatus.READY)
    db.add_all([explicit, fallback, other_owner, private, multiple, free]); db.commit()
    client = api(worker)
    response = client.get(f"/api/tasks/?scope=mine&responsible_id={worker.id}")
    assert response.status_code == 200
    assert {row["id"] for row in response.json()} == {explicit.id, fallback.id}
    response = client.get(f"/api/tasks/?scope=mine&responsible_id={other.id}")
    assert {row["id"] for row in response.json()} == {other_owner.id}
    response = client.get(f"/api/tasks/?scope=mine&responsible_id={worker.id}&priority=high&status=open")
    assert [row["id"] for row in response.json()] == [explicit.id]
    response = client.get("/api/tasks/?scope=mine&responsible_id=0")
    assert {row["id"] for row in response.json()} == {multiple.id, free.id}
    response = client.get("/api/tasks/?scope=mine")
    assert {row["id"] for row in response.json()} == {explicit.id, fallback.id, other_owner.id, multiple.id, free.id}
    assert client.get(f"/api/tasks/?scope=all&responsible_id={other.id}").status_code == 403


def test_responsible_filter_with_all_grant(db, make_user, api):
    worker = make_user(Module.TASKS, Module.TASKS_ALL)
    other = make_user(Module.TASKS)
    task = Task(title="Чужой ответственный с разрешением Все", responsible_id=other.id, status=TaskStatus.READY)
    task.assignees = [other]
    db.add(task); db.commit()
    client = api(worker)
    assert client.get(f"/api/tasks/?scope=mine&responsible_id={other.id}").json() == []
    response = client.get(f"/api/tasks/?scope=all&responsible_id={other.id}")
    assert response.status_code == 200
    assert [row["id"] for row in response.json()] == [task.id]


@pytest.mark.parametrize("value", ["-1", "unknown"])
def test_invalid_responsible_filter_rejected(api, make_user, value):
    assert api(make_user(Module.TASKS)).get(f"/api/tasks/?responsible_id={value}").status_code == 422
