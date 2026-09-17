"""Уровни доступа (0052-a): `AccessLevel` на грантах `UserModuleAccess`,
пилот — раздел «Клиенты» (`require_view`/`require_edit`/`require_full` в
`app/core/deps.py`, применённые в `app/clients/router.py`).

- `none` (нет гранта) — чтение и запись отклоняются;
- `view` — чтение проходит, запись отклоняется;
- `edit` — запись проходит, удаление отклоняется;
- `full` — проходит всё, включая удаление, без роли `ADMIN`;
- `ADMIN` — `full` на всё без явных грантов.
"""

from app.clients import service as client_service
from app.clients.models import Client
from app.clients.schemas import ClientCreate
from app.common.module_access import AccessLevel, Module
from app.cycle.models import CycleStatus
from app.tasks.models import Task, TaskLinkType, TaskStatus


def _make_client(db, name="Клиент"):
    return client_service.create_client(
        db, ClientCreate(full_name=name, phone="+70000000000", email=f"{name}@example.com")
    )


def _ready_for_deletion(db, client):
    client.cycle.status = CycleStatus.COMPLETED
    for task in db.query(Task).filter(Task.link_type == TaskLinkType.CLIENT_STAGE, Task.link_id == client.id):
        task.status = TaskStatus.DONE
    db.flush()


def test_access_level_ordering():
    assert AccessLevel.NONE < AccessLevel.VIEW < AccessLevel.EDIT < AccessLevel.FULL
    assert AccessLevel.FULL >= AccessLevel.FULL
    assert not (AccessLevel.VIEW >= AccessLevel.EDIT)


def test_user_access_level_reads_grant_and_defaults_to_none(make_user):
    no_grant = make_user()
    granted = make_user(Module.CLIENTS, level=AccessLevel.VIEW)
    admin = make_user(admin=True)

    assert no_grant.access_level(Module.CLIENTS) == AccessLevel.NONE
    assert granted.access_level(Module.CLIENTS) == AccessLevel.VIEW
    assert granted.access_level(Module.WAREHOUSE) == AccessLevel.NONE
    assert admin.access_level(Module.CLIENTS) == AccessLevel.FULL


def test_none_level_rejects_read_and_write(api, make_user, db):
    _make_client(db)
    db.commit()
    outsider = api(make_user())  # ни одного гранта на CLIENTS

    assert outsider.get("/api/clients/").status_code == 403
    resp = outsider.post(
        "/api/clients/", json={"full_name": "Новый", "phone": "+71111111111", "email": "n@example.com"}
    )
    assert resp.status_code == 403


def test_view_level_allows_read_rejects_write(api, make_user, db):
    client = _make_client(db)
    db.commit()
    viewer = api(make_user(Module.CLIENTS, level=AccessLevel.VIEW))

    assert viewer.get("/api/clients/").status_code == 200
    assert viewer.get(f"/api/clients/{client.id}").status_code == 200
    resp = viewer.patch(f"/api/clients/{client.id}/houses-count", json={"houses_count": 2})
    assert resp.status_code == 403


def test_edit_level_allows_write_rejects_delete(api, make_user, db):
    client = _make_client(db)
    _ready_for_deletion(db, client)
    db.commit()
    editor = api(make_user(Module.CLIENTS, level=AccessLevel.EDIT))

    resp = editor.patch(f"/api/clients/{client.id}/documents", json={"final_price": 1_000_000})
    assert resp.status_code == 200
    assert editor.delete(f"/api/clients/{client.id}").status_code == 403


def test_full_level_allows_delete_without_admin_role(api, make_user, db):
    client = _make_client(db)
    _ready_for_deletion(db, client)
    db.commit()
    client_id = client.id
    full_worker = api(make_user(Module.CLIENTS, level=AccessLevel.FULL))

    resp = full_worker.delete(f"/api/clients/{client_id}")
    assert resp.status_code == 204
    assert db.get(Client, client_id) is None


def test_admin_role_has_full_access_without_explicit_grant(api, make_user, db):
    client = _make_client(db)
    _ready_for_deletion(db, client)
    db.commit()
    client_id = client.id
    admin = api(make_user(admin=True))  # ни одной строки в module_access

    assert admin.get("/api/clients/").status_code == 200
    assert admin.patch(f"/api/clients/{client_id}/documents", json={"final_price": 1_000_000}).status_code == 200
    assert admin.delete(f"/api/clients/{client_id}").status_code == 204
