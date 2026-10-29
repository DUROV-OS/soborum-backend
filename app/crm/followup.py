"""Manager follow-up actions shared by the UI and approved AI proposals."""

from datetime import datetime, timedelta, timezone
from fastapi import HTTPException
from app.crm.models import CRMEntity, CRMEvent
from app.users.models import User
from app.common.module_access import Module, AccessLevel


def active(user):
    return bool(user and user.is_active and user.has_access(Module.CLIENTS))


def step_owner(db, entity, user, requested_id=None):
    candidate = db.get(User, requested_id) if requested_id else None
    if requested_id is not None and not active(candidate):
        raise HTTPException(422, "Выберите действующего сотрудника продаж")
    current = (
        db.get(User, entity.payload.get("responsible_user_id"))
        if entity.payload.get("responsible_user_id")
        else None
    )
    candidate = candidate or (current if active(current) else user)
    if not active(candidate):
        raise HTTPException(422, "Ответственный недоступен")
    if entity.kind == "deal" and not entity.archived and not active(current):
        entity.payload = {**entity.payload, "responsible_user_id": candidate.id}
        entity.version += 1
        db.add(
            CRMEvent(
                entity_id=entity.id,
                actor_id=user.id,
                action="owner_assigned",
                details={
                    "text": "Назначен ответственный при постановке следующего шага",
                    "responsible_user_id": candidate.id,
                },
            )
        )
    return candidate.id


def schedule_followup(db, user, entry_id, title, due_at=None, responsible_user_id=None, note=""):
    from app.crm.service import create, patch, out
    from app.crm.schemas import Entry, Patch
    from app.crm.guard import settings

    if user.access_level(Module.CLIENTS) < AccessLevel.EDIT:
        raise HTTPException(403, "Недостаточно прав для назначения задач продаж")
    if not title.strip() or len(title) > 1000 or len(note) > 20000:
        raise HTTPException(422, "Проверьте название и описание задачи")
    entity = (
        db.query(CRMEntity).filter_by(id=entry_id).populate_existing().with_for_update(of=CRMEntity).first()
    )
    if entity is None or entity.kind not in ("deal", "inquiry") or entity.archived:
        raise HTTPException(409, "Выберите открытую сделку или входящую заявку")
    if entity.kind == "deal":
        stage = db.get(CRMEntity, entity.payload.get("stage_id"))
        if entity.payload.get("is_unsorted") or (stage and stage.payload.get("outcome") in ("won", "lost")):
            raise HTTPException(409, "Выберите открытую принятую сделку либо входящую заявку")
    existing = [
        r
        for r in db.query(CRMEntity)
        .filter(
            CRMEntity.kind == "task",
            CRMEntity.archived.is_(False),
            CRMEntity.payload["entity_id"].as_integer() == entry_id,
        )
        .order_by(CRMEntity.id)
        if not r.payload.get("completed")
    ]
    timed = next((r for r in existing if r.payload.get("due_at")), None)
    if timed:
        assignee = (
            db.get(User, timed.payload.get("responsible_user_id"))
            if timed.payload.get("responsible_user_id")
            else None
        )
        responsible = step_owner(
            db,
            entity,
            user,
            (
                responsible_user_id
                if responsible_user_id is not None
                else assignee.id if active(assignee) else None
            ),
        )
        if not active(assignee):
            result = patch(
                db, timed.id, Patch(version=timed.version, data={"responsible_user_id": responsible}), user
            )
            return {"status": "owner_assigned", "task": result, "detail": "Срок существующей задачи сохранён"}
        db.commit()
        return {
            "status": "already_has_step",
            "task": out(timed),
            "detail": "Действующая задача со сроком сохранена без изменений",
        }
    now = datetime.now(timezone.utc)
    try:
        due = (
            datetime.fromisoformat(due_at)
            if due_at
            else now + timedelta(hours=settings(db)["response_hours"])
        )
    except (ValueError, TypeError):
        raise HTTPException(422, "Укажите корректный срок задачи")
    if due.tzinfo is None or due <= now:
        raise HTTPException(422, "Укажите будущий срок с часовым поясом")
    responsible = step_owner(db, entity, user, responsible_user_id)
    if existing:
        row = existing[0]
        result = patch(
            db,
            row.id,
            Patch(
                version=row.version,
                data={
                    "due_at": due.isoformat(),
                    "responsible_user_id": row.payload.get("responsible_user_id") or responsible,
                },
            ),
            user,
        )
        return {"status": "deadline_assigned", "task": result}
    result = create(
        db,
        "task",
        Entry(
            name=title,
            data={
                "entity_id": entry_id,
                "due_at": due.isoformat(),
                "responsible_user_id": responsible,
                "description": note,
            },
        ),
        user,
    )
    return {"status": "created", "task": result}
