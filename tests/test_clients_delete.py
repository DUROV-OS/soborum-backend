"""Удаление клиента (0030-a):

- только администратор (`DELETE /api/clients/{id}`);
- отказ 409, если цикл ещё не завершён, есть незавершённые задачи или проводки
  по клиенту — ничего не удаляется;
- при отсутствии зависимостей клиент и его заметки удаляются.
"""

from app.accounting.models import MoneyDirection, MoneyMovement, MoneySourceKind, MoneySubkind
from app.clients import service as client_service
from app.clients.models import Client
from app.clients.schemas import ClientCreate
from app.common.module_access import Module
from app.cycle.models import CycleStatus
from app.tasks.models import Task, TaskLinkType, TaskStatus


def _make_client(db, name="Клиент"):
    return client_service.create_client(
        db, ClientCreate(full_name=name, phone="+70000000000", email=f"{name}@example.com")
    )


def _close_open_tasks(db, client_id):
    for task in db.query(Task).filter(Task.link_type == TaskLinkType.CLIENT_STAGE, Task.link_id == client_id):
        task.status = TaskStatus.DONE
    db.flush()


def test_delete_requires_admin(api, make_user, db):
    client = _make_client(db)
    db.commit()
    worker = api(make_user(Module.CLIENTS))
    resp = worker.delete(f"/api/clients/{client.id}")
    assert resp.status_code == 403
    assert db.get(Client, client.id) is not None


def test_delete_rejected_with_active_cycle(api, make_user, db):
    client = _make_client(db)
    db.commit()
    admin = api(make_user(admin=True))
    resp = admin.delete(f"/api/clients/{client.id}")
    assert resp.status_code == 409
    assert db.get(Client, client.id) is not None


def test_delete_rejected_with_open_tasks(api, make_user, db):
    client = _make_client(db)
    client.cycle.status = CycleStatus.COMPLETED
    db.commit()
    admin = api(make_user(admin=True))
    resp = admin.delete(f"/api/clients/{client.id}")
    assert resp.status_code == 409
    assert db.get(Client, client.id) is not None


def test_delete_rejected_with_money_movements(api, make_user, db):
    client = _make_client(db)
    client.cycle.status = CycleStatus.COMPLETED
    _close_open_tasks(db, client.id)
    initiator = make_user(admin=True)
    db.add(
        MoneyMovement(
            direction=MoneyDirection.INCOME,
            subkind=MoneySubkind.SALE_INCOME,
            amount=1000,
            initiator_id=initiator.id,
            source_kind=MoneySourceKind.CLIENT,
            client_id=client.id,
        )
    )
    db.commit()
    admin = api(make_user(admin=True))
    resp = admin.delete(f"/api/clients/{client.id}")
    assert resp.status_code == 409
    assert db.get(Client, client.id) is not None


def test_delete_succeeds_without_dependencies(api, make_user, db):
    client = _make_client(db)
    client.cycle.status = CycleStatus.COMPLETED
    client_service.add_note(db, client, make_user(admin=True).id, "заметка")
    _close_open_tasks(db, client.id)
    db.commit()
    client_id = client.id

    admin = api(make_user(admin=True))
    resp = admin.delete(f"/api/clients/{client_id}")
    assert resp.status_code == 204
    assert db.get(Client, client_id) is None
