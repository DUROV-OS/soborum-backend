"""Список сотрудников для задач по клиенту (0111-c)."""

from app.common.module_access import AccessLevel, Module


def test_assignees_are_active_users_with_clients_access(api, make_user):
    editor = make_user(Module.CLIENTS, level=AccessLevel.EDIT)
    viewer = make_user(Module.CLIENTS, level=AccessLevel.VIEW)
    stranger = make_user(Module.WAREHOUSE, level=AccessLevel.FULL)

    resp = api(editor).get("/api/clients/assignees")
    assert resp.status_code == 200
    ids = {row["id"] for row in resp.json()}
    assert editor.id in ids and viewer.id in ids
    assert stranger.id not in ids
    assert set(resp.json()[0]) == {"id", "full_name"}  # без почты и прав


def test_assignees_skip_inactive_users(api, db, make_user):
    editor = make_user(Module.CLIENTS, level=AccessLevel.EDIT)
    gone = make_user(Module.CLIENTS, level=AccessLevel.EDIT)
    gone.is_active = False
    db.commit()

    ids = {row["id"] for row in api(editor).get("/api/clients/assignees").json()}
    assert gone.id not in ids


def test_assignees_need_edit_access(api, make_user):
    assert api(make_user(Module.CLIENTS, level=AccessLevel.VIEW)).get("/api/clients/assignees").status_code == 403
    assert api(make_user(Module.WAREHOUSE, level=AccessLevel.FULL)).get("/api/clients/assignees").status_code == 403


def test_task_goes_to_chosen_assignee(api, make_user):
    editor = make_user(Module.CLIENTS, level=AccessLevel.EDIT)
    colleague = make_user(Module.CLIENTS, level=AccessLevel.EDIT)
    client = api(editor)
    created = client.post(
        "/api/clients/", json={"full_name": "Тест Клиент", "phone": "+7 900 000-00-01", "email": "t@example.com"}
    ).json()

    resp = client.post(
        f"/api/clients/{created['id']}/tasks",
        json={"title": "Связаться", "deadline": "2030-01-15T12:30:00+00:00", "assignee_ids": [colleague.id]},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["assignee_ids"] == [colleague.id]
    assert "12:30" in body["deadline"]  # время срока не теряется

    default = client.post(
        f"/api/clients/{created['id']}/tasks", json={"title": "Ещё", "deadline": "2030-01-16T09:00:00+00:00"}
    ).json()
    assert default["assignee_ids"] == [editor.id]
