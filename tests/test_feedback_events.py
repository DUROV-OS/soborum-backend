"""Задача 0090: лента заявки — смены статуса, комментарии и «изменения в
системе» администратора, непрочитанные обновления у автора."""

import io

from app.common.module_access import Module

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


def _submit(client):
    return client.post(
        "/api/feedback/requests",
        data={"text": "Фильтр склада сбрасывается", "section": "warehouse"},
        files=[("screenshots", ("shot.png", io.BytesIO(PNG), "image/png"))],
    ).json()


def _event(client, request_id, kind="comment", text="Посмотрим на этой неделе"):
    return client.post(f"/api/feedback/requests/{request_id}/events", json={"kind": kind, "text": text})


def test_new_request_has_empty_feed(api, make_user):
    author = make_user(Module.WAREHOUSE)

    created = _submit(api(author))

    assert created["events"] == []
    assert created["unseen_updates"] == 0


def test_status_change_is_written_to_feed(api, make_user):
    author = make_user(Module.WAREHOUSE)
    admin = make_user(admin=True)
    created = _submit(api(author))

    api(admin).patch(f"/api/feedback/requests/{created['id']}", json={"status": "in_progress"})
    body = api(admin).patch(f"/api/feedback/requests/{created['id']}", json={"status": "in_progress"}).json()

    # Повторная установка того же статуса ленту не засоряет.
    assert [(e["kind"], e["old_status"], e["new_status"]) for e in body["events"]] == [
        ("status", "new", "in_progress")
    ]
    assert body["events"][0]["author"]["id"] == admin.id


def test_admin_adds_comment_and_change(api, make_user):
    author = make_user(Module.WAREHOUSE)
    admin = make_user(admin=True)
    created = _submit(api(author))

    first = _event(api(admin), created["id"])
    second = _event(api(admin), created["id"], kind="change", text="Фильтр по складу теперь сохраняется")

    assert first.status_code == 201
    assert second.status_code == 201
    assert [(e["kind"], e["text"]) for e in second.json()["events"]] == [
        ("comment", "Посмотрим на этой неделе"),
        ("change", "Фильтр по складу теперь сохраняется"),
    ]


def test_worker_cannot_add_event_even_to_own_request(api, make_user):
    author = make_user(Module.WAREHOUSE)
    created = _submit(api(author))

    assert _event(api(author), created["id"]).status_code == 403


def test_event_validation(api, make_user):
    author = make_user(Module.WAREHOUSE)
    admin = make_user(admin=True)
    created = _submit(api(author))

    assert _event(api(admin), created["id"], text="   ").status_code == 422
    assert _event(api(admin), created["id"], text="").status_code == 422
    assert _event(api(admin), created["id"], kind="status").status_code == 422
    assert _event(api(admin), created["id"], text="x" * 5001).status_code == 422
    assert _event(api(admin), 999_999).status_code == 404


def test_author_sees_unseen_updates_until_opened(api, make_user):
    author = make_user(Module.WAREHOUSE)
    admin = make_user(admin=True)
    created = _submit(api(author))
    api(admin).patch(f"/api/feedback/requests/{created['id']}", json={"status": "in_progress"})
    _event(api(admin), created["id"])

    mine = api(author).get("/api/feedback/requests?mine=true").json()
    assert mine[0]["unseen_updates"] == 2
    # Администратору чужой счётчик не показывается.
    assert api(admin).get(f"/api/feedback/requests/{created['id']}").json()["unseen_updates"] == 0

    seen = api(author).post(f"/api/feedback/requests/{created['id']}/seen")
    assert seen.status_code == 200
    assert seen.json()["unseen_updates"] == 0

    _event(api(admin), created["id"], kind="change", text="Сделали")
    assert api(author).get(f"/api/feedback/requests/{created['id']}").json()["unseen_updates"] == 1


def test_only_author_marks_request_seen(api, make_user):
    author = make_user(Module.WAREHOUSE)
    stranger = make_user(Module.WAREHOUSE)
    admin = make_user(admin=True)
    created = _submit(api(author))
    _event(api(admin), created["id"])

    assert api(admin).post(f"/api/feedback/requests/{created['id']}/seen").status_code == 403
    assert api(stranger).post(f"/api/feedback/requests/{created['id']}/seen").status_code == 403
    assert api(author).get(f"/api/feedback/requests/{created['id']}").json()["unseen_updates"] == 1


def test_stranger_cannot_read_feed(api, make_user):
    author = make_user(Module.WAREHOUSE)
    stranger = make_user(Module.WAREHOUSE)
    created = _submit(api(author))

    assert api(stranger).get(f"/api/feedback/requests/{created['id']}").status_code == 403


def test_mine_filter_limits_admin_to_own_requests(api, make_user):
    author = make_user(Module.WAREHOUSE)
    admin = make_user(admin=True)
    _submit(api(author))
    own = _submit(api(admin))

    assert len(api(admin).get("/api/feedback/requests").json()) == 2
    assert [r["id"] for r in api(admin).get("/api/feedback/requests?mine=true").json()] == [own["id"]]
    assert len(api(author).get("/api/feedback/requests?mine=true").json()) == 1


def test_admin_own_events_are_not_unseen_on_own_request(api, make_user):
    admin = make_user(admin=True)
    own = _submit(api(admin))

    body = _event(api(admin), own["id"]).json()

    assert body["unseen_updates"] == 0
