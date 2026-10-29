"""One canonical sales task mirrored into the common employee task board.

Writers lock CRMEntity before Task. Both directions share the caller's
transaction; no background refresh may overwrite a manager's changes.
"""

from datetime import datetime
from fastapi import HTTPException
from app.crm.models import CRMEntity, CRMTaskLink, CRMEvent
from app.tasks.models import Task, TaskStatus, TaskLinkType, TaskReport, TaskReportKind
from app.tasks import timelog
from app.users.models import User
from app.common.module_access import Module


def sync_to_board(db, row):
    if row.kind != "task":
        return None
    link = db.get(CRMTaskLink, row.id)
    task = (
        db.query(Task).filter_by(id=link.task_id).populate_existing().with_for_update(of=Task).first()
        if link
        else None
    )
    created = task is None
    if created:
        task = Task(
            title=row.name[:255],
            status=TaskStatus.READY,
            link_type=TaskLinkType.NONE,
            link_meta={"crm_task_id": row.id, "section": "clients"},
        )
        db.add(task)
        db.flush()
        db.add(CRMTaskLink(entity_id=row.id, task_id=task.id))
    data = row.payload
    task.title = row.name[:255]
    task.description = data.get("description") or ""
    task.deadline = datetime.fromisoformat(data["due_at"]) if data.get("due_at") else None
    user = db.get(User, data["responsible_user_id"]) if data.get("responsible_user_id") else None
    if user and (not user.is_active or not user.has_access(Module.CLIENTS)):
        user = None
    responsible_id = user.id if user else None
    if created:
        task.assignees = [user] if user else []
    elif task.responsible_id != responsible_id:
        people = [u for u in task.assignees if u.id != task.responsible_id]
        if user and user not in people:
            people.append(user)
        task.assignees = people
    task.responsible_id = responsible_id
    previous = task.status
    target = TaskStatus.DONE if row.archived or data.get("completed") else TaskStatus.READY
    # Preserve work already in progress unless the manager explicitly reopens
    # a completed task or closes it from the sales card.
    if created or target == TaskStatus.DONE or previous == TaskStatus.DONE:
        task.status = target
    db.flush()
    if not created and previous != task.status:
        timelog.record_stage(
            db, task, previous, task.status, automatic=True, note="Изменение задачи в продажах"
        )
    return task


def lock_for_board(db, task):
    id = (task.link_meta or {}).get("crm_task_id")
    if not id:
        return task
    row = (
        db.query(CRMEntity)
        .filter_by(id=id, kind="task")
        .populate_existing()
        .with_for_update(of=CRMEntity)
        .first()
    )
    if row is None or row.archived:
        raise HTTPException(409, "Задача продаж находится в архиве или недоступна")
    return db.query(Task).filter_by(id=task.id).populate_existing().with_for_update(of=Task).one()


def sync_from_board(db, task, actor=None):
    id = (task.link_meta or {}).get("crm_task_id")
    if not id:
        return
    row = db.get(CRMEntity, id)
    if row is None:
        return
    if row.archived:
        raise HTTPException(409, "Задача продаж находится в архиве")
    if not task.title.strip():
        raise HTTPException(422, "Введите название задачи")
    if task.responsible_id is None and len(task.assignees) == 1:
        task.responsible_id = task.assignees[0].id
    responsible = db.get(User, task.responsible_id) if task.responsible_id else None
    if responsible and (not responsible.is_active or not responsible.has_access(Module.CLIENTS)):
        raise HTTPException(422, "Ответственный должен иметь доступ к продажам")
    data = {
        **row.payload,
        "completed": task.status == TaskStatus.DONE,
        "due_at": task.deadline.isoformat() if task.deadline else None,
        "responsible_user_id": task.responsible_id,
        "description": task.description or "",
    }
    if data["completed"]:
        report = (
            db.query(TaskReport)
            .filter_by(task_id=task.id, kind=TaskReportKind.SUBMISSION)
            .order_by(TaskReport.id.desc())
            .first()
        )
        if report:
            data["result"] = report.comment
            if actor is None:
                actor = db.get(User, report.author_id)
    name = row.name if task.title == row.name[:255] else task.title
    changed = row.name != name or data != row.payload
    if not changed:
        return
    row.name = name
    row.payload = data
    row.version += 1
    if actor is not None:
        db.add(
            CRMEvent(
                entity_id=row.id,
                actor_id=actor.id,
                action="task_board_changed",
                details={"text": "Задача обновлена в общем списке задач", "completed": data["completed"]},
            )
        )
    db.flush()


def backfill(db):
    count = 0
    for row in db.query(CRMEntity).filter_by(kind="task").order_by(CRMEntity.id):
        if db.get(CRMTaskLink, row.id) is None:
            task = sync_to_board(db, row)
            count += 1
            from app.moysklad.models import SourceRecord

            source = (
                db.query(SourceRecord).filter_by(entity_type="amocrm/tasks", source_id=row.source_id).first()
                if row.source_id
                else None
            )
            if source and source.payload.get("created_at"):
                from datetime import timezone

                task.created_at = datetime.fromtimestamp(source.payload["created_at"], timezone.utc)
                task.link_meta = {**task.link_meta, "source_imported": True}
    return count
