from datetime import datetime, timedelta, timezone
from app.crm import service
from app.crm.models import CRMEntity, CRMTaskLink
from app.crm.schemas import Entry, Patch
from app.crm.guard import attention
from app.tasks.models import Task, TaskStatus
from app.common.module_access import Module, AccessLevel


def pipeline(db, owner):
    p = service.create(db, "pipeline", Entry(name="Продажи"), owner)
    s = service.create(db, "stage", Entry(name="Обсуждение", data={"pipeline_id": p["id"]}), owner)
    return p, s


def test_new_deal_has_a_shared_followup_and_board_completion_is_not_lost(db, make_user, api):
    owner = make_user(admin=True)
    p, s = pipeline(db, owner)
    deal = service.create(
        db, "deal", Entry(name="Дом", data={"pipeline_id": p["id"], "stage_id": s["id"]}), owner
    )
    crm = db.query(CRMEntity).filter_by(kind="task").one()
    link = db.get(CRMTaskLink, crm.id)
    assert deal["data"]["responsible_user_id"] == owner.id
    assert link is not None and service.out(crm)["board_task_id"] == link.task_id
    client = api(owner)
    assert (
        client.patch(f"/api/tasks/{link.task_id}/status", json={"status": "in_progress"}).status_code == 200
    )
    done = client.post(
        f"/api/tasks/{link.task_id}/report", data={"comment": "Позвонили, согласовали встречу"}
    )
    assert done.status_code == 200, done.text
    db.refresh(crm)
    assert crm.payload["completed"] and crm.payload["result"] == "Позвонили, согласовали встречу"
    assert (
        client.patch(
            f"/api/crm/entries/{crm.id}", json={"version": 1, "name": "Устаревшая правка"}
        ).status_code
        == 409
    )
    assert attention(db)["counts"]["no_next_step"] == 1
    reopened = service.patch(db, crm.id, Patch(version=crm.version, data={"completed": False}), owner)
    db.refresh(db.get(Task, link.task_id))
    assert db.get(Task, link.task_id).status == TaskStatus.READY
    assert "no_next_step" not in attention(db)["counts"]


def test_task_changes_permissions_and_backfill_idempotence(db, make_user, api):
    from app.crm.tasks import backfill

    owner = make_user(admin=True)
    crm = service.create(db, "task", Entry(name="Связаться", data={"responsible_user_id": owner.id}), owner)
    tid = crm["board_task_id"]
    client = api(owner)
    response = client.patch(
        f"/api/tasks/{tid}", json={"title": "Уточнить адрес", "description": "Клиент ждёт звонка"}
    )
    assert response.status_code == 200, response.text
    row = db.get(CRMEntity, crm["id"])
    db.refresh(row)
    assert row.name == "Уточнить адрес" and row.payload["description"] == "Клиент ждёт звонка"
    assert backfill(db) == 0
    denied = api(make_user(Module.TASKS))
    assert denied.patch(f"/api/tasks/{tid}", json={"title": "Нет доступа к продажам"}).status_code == 403
    client = api(owner)
    assert client.delete(f"/api/tasks/{tid}").status_code == 400
    archived = service.patch(db, row.id, Patch(version=row.version, archived=True), owner)
    assert db.get(Task, tid).status == TaskStatus.DONE
    assert client.patch(f"/api/tasks/{tid}", json={"title": "Нельзя править архив"}).status_code == 409


def test_website_first_step_survives_inquiry_acceptance(db, make_user):
    from app.clients.service import intake_lead
    from app.clients.schemas import LeadIntakePayload

    owner = make_user(admin=True)
    p, s = pipeline(db, owner)
    client, _ = intake_lead(db, LeadIntakePayload(name="Новый клиент", phone="89001112233"))
    db.commit()
    inquiry = db.query(CRMEntity).filter_by(kind="inquiry").one()
    task = db.query(CRMEntity).filter_by(kind="task").one()
    board_id = db.get(CRMTaskLink, task.id).task_id
    deal = service.accept_inquiry(
        db, inquiry, Entry(name="Дом клиента", data={"pipeline_id": p["id"], "stage_id": s["id"]}), owner
    )
    db.refresh(task)
    assert task.payload["entity_id"] == deal["id"] and task.payload["responsible_user_id"] == owner.id
    assert not task.payload["completed"]
    assert db.get(CRMTaskLink, task.id).task_id == board_id
    assert db.query(CRMEntity).filter_by(kind="task").count() == 1


def test_guard_settings_deadlines_and_terminal_deals(db, make_user, api):
    owner = make_user(admin=True)
    p, s = pipeline(db, owner)
    client = api(owner)
    assert client.put("/api/crm/settings", json={"response_hours": 2, "idle_days": 4}).status_code == 200
    assert client.put("/api/crm/settings", json={"response_hours": 0, "idle_days": 4}).status_code == 422
    started = datetime.now(timezone.utc)
    deal = service.create(
        db, "deal", Entry(name="Дом", data={"pipeline_id": p["id"], "stage_id": s["id"]}), owner
    )
    task = db.query(CRMEntity).filter_by(kind="task").one()
    due = datetime.fromisoformat(task.payload["due_at"])
    assert started + timedelta(hours=1, minutes=59) < due < started + timedelta(hours=2, minutes=1)
    assert attention(db, now=started + timedelta(hours=3))["counts"]["overdue"] == 1
    won = service.create(
        db, "stage", Entry(name="Продано", data={"pipeline_id": p["id"], "outcome": "won"}), owner
    )
    service.patch(db, deal["id"], Patch(version=deal["version"], data={"stage_id": won["id"]}), owner)
    db.refresh(task)
    assert task.payload["completed"]
    assert attention(db, now=started + timedelta(days=10))["total"] == 0
    viewer = api(make_user(Module.CLIENTS, level=AccessLevel.VIEW))
    assert viewer.get("/api/crm/attention").status_code == 200
    assert viewer.put("/api/crm/settings", json={"response_hours": 24, "idle_days": 3}).status_code == 403
