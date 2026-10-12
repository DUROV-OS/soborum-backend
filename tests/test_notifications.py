"""Задача 0080-b: хранилище уведомлений и мьюты — лента/прочитано и то, что
мьют раздела и мьют объекта блокируют создание уведомления."""

from app.common.module_access import Module
from app.notifications.models import NotificationKind
from app.notifications.service import notify


def test_notification_visible_in_list_and_unread_count(api, make_user, db):
    user = make_user(Module.CLIENTS)

    notify(db, user.id, kind=NotificationKind.CLIENT_UPDATE, module=Module.CLIENTS, title="Клиент обновлён")

    client = api(user)
    body = client.get("/api/notifications").json()
    assert len(body) == 1
    assert body[0]["title"] == "Клиент обновлён"
    assert body[0]["read_at"] is None
    assert client.get("/api/notifications/unread-count").json() == {"unread_count": 1}


def test_mark_read_clears_unread_count(api, make_user, db):
    user = make_user(Module.CLIENTS)
    notification = notify(db, user.id, kind=NotificationKind.CLIENT_UPDATE, module=Module.CLIENTS, title="t")
    client = api(user)

    response = client.post(f"/api/notifications/{notification.id}/read")

    assert response.status_code == 200
    assert response.json()["read_at"] is not None
    assert client.get("/api/notifications/unread-count").json() == {"unread_count": 0}


def test_unread_only_filter(api, make_user, db):
    user = make_user(Module.CLIENTS)
    first = notify(db, user.id, kind=NotificationKind.CLIENT_UPDATE, module=Module.CLIENTS, title="a")
    notify(db, user.id, kind=NotificationKind.CLIENT_UPDATE, module=Module.CLIENTS, title="b")
    client = api(user)
    client.post(f"/api/notifications/{first.id}/read")

    unread = client.get("/api/notifications", params={"unread_only": True}).json()

    assert [n["title"] for n in unread] == ["b"]


def test_read_all_marks_everything_read(api, make_user, db):
    user = make_user(Module.CLIENTS)
    notify(db, user.id, kind=NotificationKind.CLIENT_UPDATE, module=Module.CLIENTS, title="a")
    notify(db, user.id, kind=NotificationKind.CLIENT_UPDATE, module=Module.CLIENTS, title="b")
    client = api(user)

    response = client.post("/api/notifications/read-all")

    assert response.json() == {"unread_count": 0}
    assert client.get("/api/notifications/unread-count").json() == {"unread_count": 0}


def test_cannot_read_someone_elses_notification(api, make_user, db):
    owner = make_user(Module.CLIENTS)
    stranger = make_user(Module.CLIENTS)
    notification = notify(db, owner.id, kind=NotificationKind.CLIENT_UPDATE, module=Module.CLIENTS, title="t")

    response = api(stranger).post(f"/api/notifications/{notification.id}/read")

    assert response.status_code == 404


def test_muting_whole_module_blocks_notification(db, make_user):
    user = make_user(Module.CLIENTS)
    from app.notifications.service import set_mute

    set_mute(db, user.id, Module.CLIENTS, None, True)

    result = notify(
        db, user.id, kind=NotificationKind.CLIENT_UPDATE, module=Module.CLIENTS, title="t", object_id=1
    )

    assert result is None


def test_muting_whole_module_does_not_block_other_modules(db, make_user):
    user = make_user(Module.CLIENTS, Module.PRODUCTION)
    from app.notifications.service import set_mute

    set_mute(db, user.id, Module.CLIENTS, None, True)

    result = notify(
        db, user.id, kind=NotificationKind.PRODUCTION_UPDATE, module=Module.PRODUCTION, title="t"
    )

    assert result is not None


def test_muting_single_object_blocks_only_that_object(db, make_user):
    user = make_user(Module.CLIENTS)
    from app.notifications.service import set_mute

    set_mute(db, user.id, Module.CLIENTS, 42, True)

    muted = notify(
        db, user.id, kind=NotificationKind.CLIENT_UPDATE, module=Module.CLIENTS, title="t", object_id=42
    )
    other = notify(
        db, user.id, kind=NotificationKind.CLIENT_UPDATE, module=Module.CLIENTS, title="t", object_id=43
    )

    assert muted is None
    assert other is not None


def test_mute_api_toggle_is_idempotent_and_listable(api, make_user):
    user = make_user(Module.CLIENTS)
    client = api(user)

    client.put("/api/notifications/mutes", json={"module": "clients", "object_id": None, "muted": True})
    client.put("/api/notifications/mutes", json={"module": "clients", "object_id": None, "muted": True})
    mutes = client.get("/api/notifications/mutes").json()
    assert len(mutes) == 1
    assert mutes[0]["module"] == "clients"
    assert mutes[0]["object_id"] is None

    client.put("/api/notifications/mutes", json={"module": "clients", "object_id": None, "muted": False})
    assert client.get("/api/notifications/mutes").json() == []
