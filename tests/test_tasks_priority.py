"""Поле «Приоритет» у задачи (0070-a): по умолчанию medium, задаётся при
создании и меняется через PATCH, как остальные простые поля задачи."""


def test_create_task_defaults_to_medium_priority(api, make_user):
    admin = api(make_user(admin=True))
    resp = admin.post("/api/tasks/", json={"title": "Без приоритета", "assignee_ids": [], "reviewer_ids": []})
    assert resp.status_code == 201
    assert resp.json()["priority"] == "medium"


def test_create_task_with_explicit_priority(api, make_user):
    admin = api(make_user(admin=True))
    resp = admin.post("/api/tasks/", json={
        "title": "Срочная задача", "assignee_ids": [], "reviewer_ids": [], "priority": "high",
    })
    assert resp.status_code == 201
    assert resp.json()["priority"] == "high"


def test_update_task_changes_priority(api, make_user):
    admin = api(make_user(admin=True))
    created = admin.post("/api/tasks/", json={"title": "Задача", "assignee_ids": [], "reviewer_ids": []})
    task_id = created.json()["id"]
    assert created.json()["priority"] == "medium"

    resp = admin.patch(f"/api/tasks/{task_id}", json={"priority": "low"})
    assert resp.status_code == 200
    assert resp.json()["priority"] == "low"


def test_update_task_without_priority_keeps_current_value(api, make_user):
    admin = api(make_user(admin=True))
    created = admin.post("/api/tasks/", json={
        "title": "Задача", "assignee_ids": [], "reviewer_ids": [], "priority": "high",
    })
    task_id = created.json()["id"]

    resp = admin.patch(f"/api/tasks/{task_id}", json={"title": "Задача (переименована)"})
    assert resp.status_code == 200
    assert resp.json()["priority"] == "high"
