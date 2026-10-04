"""Удаление клиента (0030-a; уровень доступа — 0052-a):

- нужен уровень `full` на раздел «Клиенты» (`DELETE /api/clients/{id}`) —
  либо явный грант `full`, либо роль `ADMIN` (у неё `full` всегда, без грантов);
  уровня `edit` недостаточно;
- до производства (цикл `client`) и после завершения цикла удалять можно;
  отказ 409, пока дом в производстве/на монтаже, есть незакрытые задачи
  менеджера/приёма остатка или проводки по клиенту — ничего не удаляется (0095);
- автоматическая задача «перевести на следующую стадию» удаление не блокирует;
- при отсутствии зависимостей клиент и его заметки удаляются.
"""

from datetime import datetime, timedelta, timezone

from app.accounting.models import MoneyDirection, MoneyMovement, MoneySourceKind, MoneySubkind
from app.clients import service as client_service
from app.clients.models import Client
from app.clients.schemas import ClientCreate, ClientTaskCreate
from app.common.module_access import AccessLevel, Module
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


def test_delete_requires_full_level(api, make_user, db):
    client = _make_client(db)
    db.commit()
    worker = api(make_user(Module.CLIENTS, level=AccessLevel.EDIT))
    resp = worker.delete(f"/api/clients/{client.id}")
    assert resp.status_code == 403
    assert db.get(Client, client.id) is not None


def test_delete_allowed_with_full_level_without_admin_role(api, make_user, db):
    client = _make_client(db)
    client.cycle.status = CycleStatus.COMPLETED
    _close_open_tasks(db, client.id)
    db.commit()
    client_id = client.id

    worker = api(make_user(Module.CLIENTS, level=AccessLevel.FULL))
    resp = worker.delete(f"/api/clients/{client_id}")
    assert resp.status_code == 204
    assert db.get(Client, client_id) is None


def test_delete_rejected_while_house_in_production(api, make_user, db):
    client = _make_client(db)
    client.cycle.status = CycleStatus.PRODUCTION
    db.commit()
    admin = api(make_user(admin=True))
    resp = admin.delete(f"/api/clients/{client.id}")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "Нельзя удалить клиента: дом уже в производстве или на монтаже"
    assert db.get(Client, client.id) is not None


def test_delete_rejected_with_open_followup_task(api, make_user, db):
    client = _make_client(db)
    client.cycle.status = CycleStatus.COMPLETED
    _close_open_tasks(db, client.id)
    client_service.create_followup_task(
        db,
        client,
        ClientTaskCreate(title="Позвонить клиенту", deadline=datetime.now(timezone.utc) + timedelta(days=1)),
        make_user(admin=True),
    )
    db.commit()
    admin = api(make_user(admin=True))
    resp = admin.delete(f"/api/clients/{client.id}")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "Нельзя удалить клиента: сначала закройте задачу «Позвонить клиенту»"
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
