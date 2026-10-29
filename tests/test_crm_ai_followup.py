from datetime import datetime, date, timezone, timedelta
import pytest
from fastapi import HTTPException
from app.crm.models import CRMEntity, CRMTaskLink
from app.crm import service
from app.crm.schemas import Entry
from app.ai.live_tools import _schedule_sales_followup
from app.ai.guardian import authorize_tool
from app.ai.models import Chat, ChatDomain, ChatMode
from app.common.module_access import Module, AccessLevel
from app.tasks.models import Task, TaskStatus


def legacy_deal(db, owner):
    pipeline = service.create(db, "pipeline", Entry(name="Продажи"), owner)
    stage = service.create(db, "stage", Entry(name="Новый", data={"pipeline_id": pipeline["id"]}), owner)
    row = CRMEntity(
        kind="deal",
        name="Старый лид",
        source_id="legacy-123",
        archived=False,
        payload={
            "pipeline_id": pipeline["id"],
            "stage_id": stage["id"],
            "amount": 100,
            "responsible_user_id": None,
        },
    )
    db.add(row)
    db.commit()
    return row


def test_approved_followup_uses_shared_tasks_and_does_not_duplicate_or_postpone(db, make_user):
    owner = make_user(admin=True)
    deal = legacy_deal(db, owner)
    result = _schedule_sales_followup(
        db, owner, deal.id, "Позвонить по заявке", note="Уточнить удобное время"
    )
    assert result["status"] == "created"
    assert result["task"]["board_task_id"]
    db.refresh(deal)
    assert deal.payload["responsible_user_id"] == owner.id
    due = result["task"]["data"]["due_at"]
    again = _schedule_sales_followup(
        db,
        owner,
        deal.id,
        "Другая задача",
        due_at=(datetime.now(timezone.utc) + timedelta(days=10)).isoformat(),
    )
    assert again["status"] == "already_has_step"
    assert again["task"]["id"] == result["task"]["id"] and again["task"]["data"]["due_at"] == due
    assert db.query(CRMTaskLink).count() == 1


def test_ai_followup_requires_edit_and_human_approval_and_valid_deadline(db, make_user):
    owner = make_user(admin=True)
    deal = legacy_deal(db, owner)
    args = {"entry_id": deal.id, "title": "Позвонить"}
    chat = Chat(owner_id=owner.id, domain=ChatDomain.CLIENTS, mode=ChatMode.REQUIRE_APPROVAL)
    with pytest.raises(HTTPException):
        authorize_tool(chat, owner, "schedule_sales_followup", args)
    assert authorize_tool(chat, owner, "schedule_sales_followup", args, approved=True)
    chat.mode = ChatMode.NO_ACTIONS
    with pytest.raises(HTTPException):
        authorize_tool(chat, owner, "schedule_sales_followup", args, approved=True)
    viewer = make_user(Module.AI, Module.CLIENTS, level=AccessLevel.VIEW)
    with pytest.raises(HTTPException):
        _schedule_sales_followup(db, viewer, deal.id, "Нельзя")
    with pytest.raises(HTTPException):
        _schedule_sales_followup(db, owner, deal.id, "Позвонить", due_at="2001-01-01T00:00:00+03:00")
    db.refresh(deal)
    assert deal.payload["responsible_user_id"] is None
    assert db.query(CRMTaskLink).count() == 0


def test_imported_completion_without_timestamp_does_not_invent_kpi(db, make_user, monkeypatch):
    from app.accounting import service as accounting

    owner = make_user(admin=True)
    monkeypatch.setattr(accounting, "_utcnow", lambda: datetime(2026, 10, 29, tzinfo=timezone.utc))
    task = Task(
        title="Импортированная выполненная задача",
        status=TaskStatus.DONE,
        deadline=datetime(2026, 10, 20, tzinfo=timezone.utc),
        link_meta={"source_imported": True},
    )
    task.assignees = [owner]
    db.add(task)
    db.commit()
    result = accounting._compute_kpi_for_period(db, owner.id, date(2026, 10, 1), date(2026, 11, 1))
    assert result.tasks_total == 0 and result.kpi is None


def test_disabled_owner_is_visible_and_approved_followup_repairs_assignment(db, make_user):
    from app.crm.guard import attention

    owner = make_user(admin=True)
    worker = make_user(Module.CLIENTS, Module.TASKS)
    deal = legacy_deal(db, owner)
    deal.payload = {**deal.payload, "responsible_user_id": worker.id}
    db.commit()
    first = _schedule_sales_followup(db, owner, deal.id, "Позвонить", responsible_user_id=worker.id)
    worker.is_active = False
    db.commit()
    assert attention(db, reason="no_owner")["total"] == 2
    repaired = _schedule_sales_followup(db, owner, deal.id, "Позвонить")
    db.refresh(deal)
    assert deal.payload["responsible_user_id"] == owner.id
    assert repaired["task"]["data"]["responsible_user_id"] == owner.id
    assert repaired["task"]["data"]["due_at"] == first["task"]["data"]["due_at"]
    assert attention(db, reason="no_owner")["total"] == 0
