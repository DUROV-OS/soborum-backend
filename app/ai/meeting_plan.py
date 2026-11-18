"""Meeting proposals never create commitments until the owner approves them.

The processing row is also the serialization lock for edits and approval.
Tasks, time blocks and the approval receipt commit in one transaction.
"""
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from pydantic import Field, model_validator
from sqlalchemy.orm import Session

from app.accounting.evidence_schemas import Strict
from app.ai.models import Meeting, MeetingProcessing, MeetingStatus
from app.common.module_access import Module, AccessLevel
from app.users.models import User


class Action(Strict):
    id: UUID
    title: str = Field(min_length=1, max_length=200)
    expected_result: str = Field(default='', max_length=2000)
    assignee_id: int | None = Field(default=None, gt=0)
    reviewer_id: int | None = Field(default=None, gt=0)
    deadline: datetime | None = None
    duration_minutes: int | None = Field(default=None, ge=5, le=480)
    scheduled_start: datetime | None = None
    source_line_ids: list[int] = Field(default_factory=list, max_length=30)
    quote: str = Field(default='', max_length=3000)
    clarification: str = Field(default='', max_length=2000)
    selected: bool = True

    @model_validator(mode='after')
    def valid(self):
        if not self.title.strip(): raise ValueError('Укажите поручение')
        for at in (self.deadline, self.scheduled_start):
            if at and at.tzinfo is None: raise ValueError('Укажите время с часовым поясом')
        if self.scheduled_start and not self.duration_minutes:
            raise ValueError('Для календаря укажите длительность')
        if self.scheduled_start and self.deadline and self.scheduled_start + timedelta(minutes=self.duration_minutes) > self.deadline:
            raise ValueError('Работа должна закончиться до срока задачи')
        return self


class PlanEdit(Strict):
    version: int = Field(ge=1)
    actions: list[Action] = Field(max_length=100)

    @model_validator(mode='after')
    def unique(self):
        if len({a.id for a in self.actions}) != len(self.actions): raise ValueError('Поручения повторяются')
        return self


class Approval(Strict):
    version: int = Field(ge=1)
    request_id: UUID
    action_ids: list[UUID] = Field(min_length=1, max_length=100)


class Slots(Strict):
    action_id: UUID
    start: datetime
    end: datetime

    @model_validator(mode='after')
    def valid(self):
        if self.start.tzinfo is None or self.end.tzinfo is None or not timedelta(0) < self.end-self.start <= timedelta(days=14):
            raise ValueError('Выберите период до 14 дней с часовым поясом')
        return self


def enqueue(db: Session, meeting: Meeting):
    job = db.get(MeetingProcessing, meeting.id)
    if job is None:
        job = MeetingProcessing(meeting_id=meeting.id, status='queued', attempts=0, error='', version=1, data={})
        db.add(job)
    return job


def output(job):
    if job is None: return None
    return dict(status=job.status, error=job.error, version=job.version, updated_at=job.updated_at,
                attempts=job.attempts, **{k: v for k, v in job.data.items() if k in
                ('actions', 'recommendations', 'approvals', 'model', 'transcript_ready', 'speech_model')})


def locked(db, meeting):
    job = db.query(MeetingProcessing).filter_by(meeting_id=meeting.id).populate_existing().with_for_update().first()
    if not job or job.status != 'ready': raise HTTPException(409, 'Дождитесь готовности протокола')
    return job


def require_task_edit(user):
    if user.access_level(Module.TASKS) < AccessLevel.EDIT:
        raise HTTPException(403, 'Для распределения поручений нужен доступ к редактированию задач')


def retry(db, meeting):
    # Lock the meeting before creating a row, so simultaneous first retries are idempotent.
    db.query(Meeting).filter_by(id=meeting.id).populate_existing().with_for_update().one()
    if meeting.status != MeetingStatus.FINISHED or not meeting.audio_file_id:
        raise HTTPException(409, 'Сначала сохраните и завершите аудиозапись')
    job = enqueue(db, meeting)
    if job.status == 'error':
        job.status, job.attempts, job.error, job.lease_until, job.lease_token = 'queued', 0, '', None, None
        job.version += 1
    db.commit()
    return output(job)


def save(db, meeting, body):
    job = locked(db, meeting)
    if job.version != body.version: raise HTTPException(409, 'План изменён. Обновите страницу.')
    before = {a['id']: a for a in job.data.get('actions', [])}
    after = {str(a.id): a.model_dump(mode='json') for a in body.actions}
    if set(before) != set(after): raise HTTPException(422, 'Сохраняйте состав поручений; ненужные снимите с распределения')
    for id, old in before.items():
        for key in ('source_line_ids', 'quote'):
            if after[id][key] != old[key]: raise HTTPException(422, 'Источник поручения нельзя подменять')
        if old.get('task_id'):
            candidate = {k: v for k, v in old.items() if k in Action.model_fields}
            if after[id] != candidate: raise HTTPException(409, 'Утверждённое поручение изменяйте в его задаче')
            after[id] = old
    job.data = {**job.data, 'actions': list(after.values())}
    job.version += 1
    db.commit()
    return output(job)


def busy_intervals(db, user_id, start, end):
    from app.collaboration.calendar import Event, occurrences, busy_people
    result = []
    for row in db.query(Event):
        data = row.data
        if data.get('deleted') or data.get('cancelled') or data.get('availability') == 'free': continue
        # Work blocks occupy the executor's time; organizers/reviewers retain visibility.
        involved = busy_people(row)
        if user_id in involved and (user_id == row.owner_id or data.get('responses', {}).get(str(user_id)) != 'declined'):
            result.extend(occurrences(data, start, end))
    return result


def preferences(person):
    from app.collaboration.calendar_preferences import get_preferences
    return get_preferences(person)


def within_work_hours(person, start, end):
    prefs = preferences(person)
    zone = ZoneInfo(prefs.timezone)
    a, b = start.astimezone(zone), end.astimezone(zone)
    return (a.date() == b.date() and a.weekday() in prefs.weekdays
            and a.hour*60+a.minute >= prefs.work_start
            and b.hour*60+b.minute <= prefs.work_end)


def slots(db, meeting, body):
    job = locked(db, meeting)
    action = next((a for a in job.data.get('actions', []) if a['id'] == str(body.action_id)), None)
    if not action: raise HTTPException(404, 'Поручение не найдено')
    a = Action.model_validate({k: v for k, v in action.items() if k in Action.model_fields})
    person = db.get(User, a.assignee_id) if a.assignee_id else None
    if not person or not person.is_active or not a.duration_minutes:
        raise HTTPException(422, 'Сначала выберите исполнителя и оцените длительность')
    start = max(body.start, datetime.now(timezone.utc))
    end = min(body.end, a.deadline) if a.deadline else body.end
    intervals = busy_intervals(db, person.id, start, end) if end > start else []
    for other in job.data.get('actions', []):
        if other['id'] == action['id'] or other.get('task_id') or not other.get('selected') or other.get('assignee_id') != person.id: continue
        if other.get('scheduled_start') and other.get('duration_minutes'):
            at = datetime.fromisoformat(other['scheduled_start'])
            intervals.append((at, at+timedelta(minutes=other['duration_minutes'])))
    result = []
    at = start.replace(second=0, microsecond=0)
    at += timedelta(minutes=(15-at.minute%15)%15)
    if at < start: at += timedelta(minutes=15)
    while at+timedelta(minutes=a.duration_minutes) <= end and len(result) < 12:
        finish = at+timedelta(minutes=a.duration_minutes)
        if within_work_hours(person, at, finish) and not any(x < finish and y > at for x, y in intervals):
            result.append({'start': at.isoformat(), 'end': finish.isoformat()})
        at += timedelta(minutes=15)
    return {'slots': result, 'checked_at': datetime.now(timezone.utc), 'notice': 'Время будет повторно проверено при утверждении'}


def approve(db, user, meeting, body):
    require_task_edit(user)
    job = locked(db, meeting)
    request = body.model_dump(mode='json')
    receipts = job.data.get('approvals', [])
    receipt = next((r for r in receipts if r['request']['request_id'] == request['request_id']), None)
    if receipt:
        if receipt['request'] != request: raise HTTPException(409, 'Запрос уже использован с другим содержимым')
        return output(job)
    if job.version != body.version: raise HTTPException(409, 'План изменён. Обновите страницу.')
    ids = {str(id) for id in body.action_ids}
    actions = [dict(a) for a in job.data.get('actions', [])]
    if len(ids) != len(body.action_ids) or not ids.issubset({a['id'] for a in actions}):
        raise HTTPException(422, 'Проверьте список поручений')
    from app.tasks.service import create_task
    from app.collaboration.calendar import Event, Change, Input
    from app.warehouse.documents import advisory
    # Serialize scheduling with calendar writes. Acquire locks in the same order.
    advisory(db, 'team-calendar')
    pending_intervals = []
    for item in actions:
        if item['id'] not in ids or item.get('task_id'): continue
        a = Action.model_validate({k: v for k, v in item.items() if k in Action.model_fields})
        if not a.selected: raise HTTPException(422, 'Поручение исключено из распределения')
        if not a.assignee_id or not a.reviewer_id or a.assignee_id == a.reviewer_id:
            raise HTTPException(422, 'Выберите исполнителя и другого сотрудника для приёмки')
        if not a.expected_result.strip(): raise HTTPException(422, 'Укажите ожидаемый результат')
        for person_id in (a.assignee_id, a.reviewer_id):
            person = db.get(User, person_id)
            if not person or not person.is_active or person.access_level(Module.TASKS) < AccessLevel.EDIT:
                raise HTTPException(422, 'Исполнитель и проверяющий должны иметь доступ к работе с задачами')
        person = db.get(User, a.assignee_id)
        start, end = a.scheduled_start, None
        if start:
            end = start+timedelta(minutes=a.duration_minutes)
            if start < datetime.now(timezone.utc): raise HTTPException(422, 'Выберите будущее время работы')
            if not within_work_hours(person, start, end): raise HTTPException(409, 'Время вне рабочего графика исполнителя')
            busy = busy_intervals(db, person.id, start, end) + [(x, y) for p, x, y in pending_intervals if p == person.id]
            if any(x < end and y > start for x, y in busy): raise HTTPException(409, 'Время исполнителя уже занято. Подберите другое окно.')
            pending_intervals.append((person.id, start, end))
        description = f'{a.expected_result}\n\nОснование: совещание №{meeting.id}\n{a.quote}'
        task = create_task(db, title=a.title.strip(), description=description, deadline=a.deadline,
                           assignee_ids=[a.assignee_id], reviewer_ids=[a.reviewer_id], responsible_id=user.id,
                           link_meta={'meeting_id': meeting.id, 'meeting_action_id': item['id'], 'source_line_ids': a.source_line_ids})
        item.update(task_id=task.id, approved_by=user.id, approved_at=datetime.now(timezone.utc).isoformat())
        from app.notifications.models import Notification, NotificationKind
        from app.notifications.service import is_muted
        for person_id in {a.assignee_id, a.reviewer_id}:
            if not is_muted(db, person_id, Module.TASKS, task.id):
                db.add(Notification(user_id=person_id, kind=NotificationKind.AGENT_INITIATIVE,
                    title=f'Поручение с совещания: {a.title}'[:255],
                    body='Вы исполнитель.' if person_id == a.assignee_id else 'Вы принимаете результат.',
                    object_type='tasks', object_id=task.id))
        if start:
            data = Input(request_id=uuid4(), title=a.title, description=description, start=start, end=end,
                         timezone=preferences(person).timezone, category='work', participants=sorted({a.assignee_id, a.reviewer_id}),
                         task_assignee_id=a.assignee_id, task_reviewer_id=a.reviewer_id, reminder_minutes=15).model_dump(mode='json', exclude={'request_id', 'version'})
            data.update(task_id=task.id, responses={str(p): 'pending' for p in data['participants']}, attachments=[])
            event = Event(owner_id=user.id, data=data)
            db.add(event); db.flush()
            db.add(Change(event_id=event.id, actor_id=user.id, request_id=str(uuid4()), data={
                'route_id': None, 'request': {'source': 'meeting', 'meeting_id': meeting.id, 'action_id': item['id']},
                'before': None, 'after': data, 'at': datetime.now(timezone.utc).isoformat()}))
            task.link_meta = {**task.link_meta, 'calendar_event_id': event.id, 'calendar_start': data['start'],
                              'calendar_participants': sorted({user.id, *data['participants']}), 'calendar_deleted': False}
            item['event_id'] = event.id
    job.data = {**job.data, 'actions': actions, 'approvals': [*receipts, {'request': request, 'actor_id': user.id, 'at': datetime.now(timezone.utc).isoformat()}]}
    job.version += 1
    db.commit()
    return output(job)
