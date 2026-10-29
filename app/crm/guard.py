"""Deterministic sales follow-up controls; model output cannot suppress an alert."""

from datetime import datetime, timedelta, timezone
from collections import Counter
from sqlalchemy import func
from app.crm.models import CRMEntity, CRMSettings, CRMEvent
from app.crm.schemas import TaskData
from app.crm.tasks import sync_to_board
from app.users.models import User
from app.common.module_access import Module
from app.moysklad.models import SourceRecord


def settings(db):
    row = db.get(CRMSettings, 1)
    return {"response_hours": row.response_hours if row else 24, "idle_days": row.idle_days if row else 3}


def ensure_followup(db, entity, *, key, responsible_id=None, due_at=None):
    old = db.query(CRMEntity).filter_by(kind="task", source_id=key).first()
    if old:
        return old
    user = db.get(User, responsible_id) if responsible_id else None
    if user and (not user.is_active or not user.has_access(Module.CLIENTS)):
        user = None
    task = CRMEntity(
        kind="task",
        source_id=key,
        name=("Связаться: " + entity.name)[:1000],
        archived=False,
        payload=TaskData(
            entity_id=entity.id,
            responsible_user_id=user.id if user else None,
            due_at=due_at or datetime.now(timezone.utc) + timedelta(hours=settings(db)["response_hours"]),
        ).model_dump(mode="json"),
    )
    db.add(task)
    db.flush()
    sync_to_board(db, task)
    return task


def accepted(db, inquiry, deal, user):
    tasks = (
        db.query(CRMEntity)
        .filter(
            CRMEntity.kind == "task",
            CRMEntity.archived.is_(False),
            CRMEntity.payload["entity_id"].as_integer() == inquiry.id,
        )
        .all()
    )
    if tasks:
        for task in tasks:
            if task.payload.get("completed"):
                continue
            task.payload = {
                **task.payload,
                "entity_id": deal.id,
                "responsible_user_id": task.payload.get("responsible_user_id")
                or deal.payload.get("responsible_user_id")
                or user.id,
            }
            task.version += 1
            sync_to_board(db, task)
    else:
        ensure_followup(
            db,
            deal,
            key="followup/inquiry/" + str(inquiry.id),
            responsible_id=deal.payload.get("responsible_user_id") or user.id,
        )


def close_terminal_followups(db, deal, user):
    stage = db.get(CRMEntity, deal.payload.get("stage_id"))
    if not stage or stage.payload.get("outcome") not in ("won", "lost"):
        return
    for task in db.query(CRMEntity).filter(
        CRMEntity.kind == "task",
        CRMEntity.archived.is_(False),
        CRMEntity.payload["entity_id"].as_integer() == deal.id,
    ):
        if task.payload.get("completed") or not (task.source_id or "").startswith(
            ("followup/", "website-task/")
        ):
            continue
        task.payload = {**task.payload, "completed": True, "result": "Сделка закрыта: " + stage.name}
        task.version += 1
        sync_to_board(db, task)
        db.add(
            CRMEvent(
                entity_id=task.id,
                actor_id=user.id,
                action="closed_with_deal",
                details={"text": "Контрольная задача закрыта вместе со сделкой"},
            )
        )


def aware(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def attention(db, *, now=None, offset=0, limit=50):
    now = aware(now or datetime.now(timezone.utc))
    rules = settings(db)
    rows = db.query(CRMEntity).filter(CRMEntity.archived.is_(False)).all()
    stages = {r.id: r for r in rows if r.kind == "stage"}
    tasks = [r for r in rows if r.kind == "task" and not r.payload.get("completed")]
    by_entity = {}
    for task in tasks:
        by_entity.setdefault(task.payload.get("entity_id"), []).append(task)
    activity = {
        id: aware(time)
        for id, time in db.query(CRMEvent.entity_id, func.max(CRMEvent.created_at)).group_by(
            CRMEvent.entity_id
        )
    }
    source_activity = {
        r.source_id: datetime.fromtimestamp(
            r.payload.get("updated_at") or r.payload.get("created_at") or 0, timezone.utc
        )
        for r in db.query(SourceRecord).filter_by(entity_type="amocrm/leads")
    }
    signals = []

    def add(row, reason, title, due=None):
        signals.append(
            {
                "entry_id": row.id,
                "kind": row.kind,
                "name": row.name,
                "reason": reason,
                "title": title,
                "responsible_user_id": row.payload.get("responsible_user_id"),
                "due_at": due.isoformat() if due else None,
            }
        )

    for row in rows:
        if row.kind == "inquiry":
            add(row, "inquiry", "Обработать входящее обращение")
            linked = by_entity.get(row.id, [])
            if linked and not any(t.payload.get("responsible_user_id") for t in linked):
                add(row, "no_owner", "Назначить ответственного за обращение")
        if row.kind == "task" and not row.payload.get("completed") and row.payload.get("due_at"):
            due = aware(row.payload["due_at"])
            if due < now:
                add(row, "overdue", "Просрочена задача по лиду", due)
        if row.kind != "deal" or row.payload.get("is_unsorted"):
            continue
        stage = stages.get(row.payload.get("stage_id"))
        if stage and stage.payload.get("outcome") in ("won", "lost"):
            continue
        next_tasks = by_entity.get(row.id, [])
        if not next_tasks:
            add(row, "no_next_step", "Нет следующего шага")
        elif not any(t.payload.get("due_at") for t in next_tasks):
            add(row, "no_deadline", "У следующего шага нет срока")
        if not row.payload.get("responsible_user_id"):
            add(row, "no_owner", "Не назначен ответственный")
        last = activity.get(row.id) or source_activity.get(row.source_id) or aware(row.created_at)
        scheduled = any(t.payload.get("due_at") and aware(t.payload["due_at"]) >= now for t in next_tasks)
        if not scheduled and last < now - timedelta(days=rules["idle_days"]):
            add(row, "idle", "Нет активности по сделке")
    priority = {"overdue": 0, "inquiry": 1, "no_next_step": 2, "no_deadline": 3, "no_owner": 4, "idle": 5}
    signals.sort(key=lambda s: (priority[s["reason"]], s.get("due_at") or "", -s["entry_id"]))
    return {
        "total": len(signals),
        "counts": dict(Counter(s["reason"] for s in signals)),
        "items": signals[offset : offset + limit],
        "settings": rules,
        "checked_at": now.isoformat(),
    }
