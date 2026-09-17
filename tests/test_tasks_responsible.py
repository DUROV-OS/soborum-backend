"""Поле «Ответственный» у задачи (0066-g): отдельно от assignees/reviewers,
не обязан быть среди исполнителей, редактируется через create/update как
обычное поле, доступно у любой задачи (не только производства)."""

from app.common.module_access import Module


def test_create_task_with_responsible_not_among_assignees(api, make_user):
    admin = api(make_user(admin=True))
    assignee = make_user(Module.TASKS)
    responsible = make_user(Module.TASKS)

    resp = admin.post("/api/tasks/", json={
        "title": "Задача с ответственным",
        "assignee_ids": [assignee.id],
        "reviewer_ids": [],
        "responsible_id": responsible.id,
    })
    assert resp.status_code == 201
    body = resp.json()
    assert body["responsible"]["id"] == responsible.id
    assert responsible.id not in [a["id"] for a in body["assignees"]]


def test_create_task_without_responsible_leaves_it_null(api, make_user):
    admin = api(make_user(admin=True))
    resp = admin.post("/api/tasks/", json={"title": "Без ответственного", "assignee_ids": [], "reviewer_ids": []})
    assert resp.status_code == 201
    assert resp.json()["responsible"] is None


def test_create_task_rejects_unknown_responsible(api, make_user):
    admin = api(make_user(admin=True))
    resp = admin.post("/api/tasks/", json={
        "title": "Задача", "assignee_ids": [], "reviewer_ids": [], "responsible_id": 999999,
    })
    assert resp.status_code == 400


def test_update_task_sets_responsible(api, make_user):
    admin = api(make_user(admin=True))
    responsible = make_user(Module.TASKS)
    created = admin.post("/api/tasks/", json={"title": "Задача", "assignee_ids": [], "reviewer_ids": []})
    task_id = created.json()["id"]
    assert created.json()["responsible"] is None

    resp = admin.patch(f"/api/tasks/{task_id}", json={"responsible_id": responsible.id})
    assert resp.status_code == 200
    assert resp.json()["responsible"]["id"] == responsible.id
