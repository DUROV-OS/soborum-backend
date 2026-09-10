"""Сверка задач смены стадии клиента с реальностью
(app.clients.reconcile):

- клиентам, у которых стадия выставлена в обход сервиса (сид, ручные правки),
  сверка заводит недостающую задачу «перевести на следующую стадию»;
- клиенту на последней стадии открытые задачи закрываются;
- дубли и задачи под уже пройденную стадию закрываются, остаётся ровно одна;
- прогон идемпотентен;
- ручной эндпоинт `POST /api/clients/reconcile-stage-tasks` — только админу.
"""

import pytest

from app.clients import service as client_service
from app.clients.models import ClientStage
from app.clients.reconcile import reconcile_client_stage_tasks
from app.clients.schemas import ClientCreate
from app.common.module_access import Module
from app.tasks import service as task_service
from app.tasks.models import Task, TaskLinkType, TaskStatus


def _make_client(db, name="Клиент"):
    return client_service.create_client(
        db, ClientCreate(full_name=name, phone="+70000000000", email=f"{name}@example.com")
    )


def _open_stage_tasks(db, client_id):
    return (
        db.query(Task)
        .filter(
            Task.link_type == TaskLinkType.CLIENT_STAGE,
            Task.link_id == client_id,
            Task.status != TaskStatus.DONE,
        )
        .all()
    )


def test_backfills_missing_task_for_stage_set_out_of_band(db):
    client = _make_client(db, "Старый")
    # имитируем сид: стадия ушла вперёд, задача не заведена
    for task in _open_stage_tasks(db, client.id):
        task_service.force_close(db, task)
    client.stage = ClientStage.APPROVAL
    db.flush()
    assert _open_stage_tasks(db, client.id) == []

    report = reconcile_client_stage_tasks(db)

    assert report["created"] == 1
    open_tasks = _open_stage_tasks(db, client.id)
    assert len(open_tasks) == 1
    assert open_tasks[0].link_meta["stage"] == "approval"

    # второй прогон ничего не делает
    assert reconcile_client_stage_tasks(db) == {
        "checked": 1, "created": 0, "closed_stale": 0, "closed_final": 0, "deduped": 0
    }


def test_closes_open_task_on_final_stage(db):
    client = _make_client(db, "Финал")
    client.stage = ClientStage.POSTPAYMENT
    db.flush()
    assert len(_open_stage_tasks(db, client.id)) == 1  # задача с «lead» осталась висеть

    report = reconcile_client_stage_tasks(db)

    assert report["closed_final"] == 1
    assert _open_stage_tasks(db, client.id) == []


def test_dedupes_and_closes_stale_stage_task(db):
    client = _make_client(db, "Дубли")  # 1 задача под «lead»
    client.stage = ClientStage.APPROVAL
    db.flush()
    # ещё две открытые задачи под текущую стадию (дубли)
    for _ in range(2):
        task_service.create_link_task(
            db,
            title="дубль",
            link_type=TaskLinkType.CLIENT_STAGE,
            link_id=client.id,
            assignees=[],
            link_meta={"stage": "approval"},
        )
    assert len(_open_stage_tasks(db, client.id)) == 3  # 1 stale («lead») + 2 дубля

    report = reconcile_client_stage_tasks(db)

    assert report["closed_stale"] == 1
    assert report["deduped"] == 1
    open_tasks = _open_stage_tasks(db, client.id)
    assert len(open_tasks) == 1
    assert open_tasks[0].link_meta["stage"] == "approval"


def test_reconcile_endpoint_is_admin_only(api, make_user):
    worker = make_user(Module.CLIENTS)
    admin = make_user(Module.CLIENTS, admin=True)

    assert api(worker).post("/api/clients/reconcile-stage-tasks").status_code == 403

    res = api(admin).post("/api/clients/reconcile-stage-tasks")
    assert res.status_code == 200
    assert set(res.json()) == {"checked", "created", "closed_stale", "closed_final", "deduped"}
