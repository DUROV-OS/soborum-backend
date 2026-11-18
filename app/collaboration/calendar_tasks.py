"""Calendar owns task details; the task board owns execution and review history.

All writes lock event before task. The two records are committed atomically.
"""
from datetime import datetime,timezone
from fastapi import HTTPException
from sqlalchemy.orm import object_session
from app.common.module_access import Module, AccessLevel
from app.tasks.models import Task
from app.users.models import User


def visible_task(task,user):
    meta=task.link_meta or {}
    return not meta.get('calendar_event_id') or (not meta.get('calendar_deleted') and user.id in meta.get('calendar_participants',[]))


def require_visible(task,user):
    if not visible_task(task,user): raise HTTPException(404,'Задача не найдена')


def lock_for_board(db,task,user):
    from .calendar import Event,visible
    event=db.query(Event).filter_by(id=task.link_meta['calendar_event_id']).populate_existing().with_for_update().first()
    if not event or event.data.get('deleted') or not visible(event,user): raise HTTPException(404,'Задача не найдена')
    return db.query(Task).filter_by(id=task.id).populate_existing().with_for_update().one()


def task_summary(row,user):
    if not row.data.get('task_id'): return None
    task=object_session(row).get(Task,row.data['task_id'])
    if not task: return None
    return {'id':task.id,'status':task.status.value,'can_open':user.has_access(Module.TASKS)}


def sync_task(db,user,row,data,before):
    task_id=(before or {}).get('task_id')
    # Legacy work-category appointments remain appointments until explicitly assigned.
    requested=data.get('task_assignee_id') or data.get('task_reviewer_id')
    if not task_id and not requested: return data
    if user.access_level(Module.TASKS)<AccessLevel.EDIT: raise HTTPException(403,'Для создания и изменения задачи нужны права редактирования задач')
    if data['category']!='work' or data['recurrence']!='none': raise HTTPException(422,'Для задачи выберите тип «Задача» без повторения')
    assignee,reviewer=data.get('task_assignee_id'),data.get('task_reviewer_id')
    if not assignee or not reviewer or assignee==reviewer: raise HTTPException(422,'Выберите исполнителя и другого сотрудника для приёмки')
    for id in (assignee,reviewer):
        person=db.get(User,id)
        if not person or not person.is_active or person.access_level(Module.TASKS)<AccessLevel.EDIT: raise HTTPException(422,'Исполнителю и проверяющему нужен доступ к работе с задачами')
    if not {assignee,reviewer}.issubset({row.owner_id,*data['participants']}): raise HTTPException(422,'Исполнитель и проверяющий должны быть участниками записи')
    from app.tasks.service import create_task,update_task
    deadline=datetime.fromisoformat(data['end']).astimezone(timezone.utc)
    meta={'calendar_event_id':row.id,'calendar_start':data['start'],'calendar_participants':sorted({row.owner_id,*data['participants']}),'calendar_deleted':bool(data['cancelled'])}
    if task_id:
        task=db.query(Task).filter_by(id=task_id).populate_existing().with_for_update().one()
        # A work block is time reserved for execution, not the meeting task's due date.
        if (task.link_meta or {}).get('meeting_id'):
            deadline=task.deadline
        # Preserve the report's author; the organizer may replace a reviewer
        # while acceptance is pending (e.g. the previous reviewer left).
        if (task.status.value in ('in_review','done') and assignee!=(before or {}).get('task_assignee_id')) or (task.status.value=='done' and reviewer!=(before or {}).get('task_reviewer_id')):
            raise HTTPException(409,'Нельзя менять исполнителя после сдачи и проверяющего после приёмки')
        update_task(db,task,title=data['title'],description=data['description'],deadline=deadline,assignee_ids=[assignee],reviewer_ids=[reviewer],responsible_id=row.owner_id,depends_on_ids=None,image_ids=None)
        task.link_meta={**(task.link_meta or {}),**meta}
    else:
        task=create_task(db,title=data['title'],description=data['description'],deadline=deadline,assignee_ids=[assignee],reviewer_ids=[reviewer],responsible_id=row.owner_id,link_meta=meta)
    return {**data,'task_id':task.id}


def archive_task(db,row):
    if row.data.get('task_id'):
        task=db.query(Task).filter_by(id=row.data['task_id']).populate_existing().with_for_update().one()
        task.link_meta={**(task.link_meta or {}),'calendar_deleted':True}
