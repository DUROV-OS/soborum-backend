"""Internal calendar. Invitations are internal; no email delivery is implied."""
from datetime import datetime, timedelta, timezone
from calendar import monthrange
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import Field, model_validator, HttpUrl, ValidationError
from sqlalchemy import ForeignKey, JSON, String, Integer
from sqlalchemy.orm import Mapped, mapped_column, Session
from app.db.base import Base
from app.db.session import get_db
from app.core.deps import get_current_user
from app.users.models import User
from app.accounting.evidence_schemas import Strict
from app.warehouse.documents import advisory

router = APIRouter(prefix='/calendar')
from .calendar_preferences import router as preferences_router
router.include_router(preferences_router)

class Event(Base):
    __tablename__ = 'team_calendar_events'
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    data: Mapped[dict] = mapped_column(JSON)

class Change(Base):
    __tablename__ = 'team_calendar_changes'
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey('team_calendar_events.id'), index=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    request_id: Mapped[str] = mapped_column(String(36), unique=True)
    data: Mapped[dict] = mapped_column(JSON)

class DocumentLink(Strict):
    label: str = Field(min_length=1,max_length=120)
    url: HttpUrl

class Input(Strict):
    request_id: UUID
    version: int = Field(default=0, ge=0)
    title: str = Field(min_length=1, max_length=200)
    start: datetime
    end: datetime
    timezone: str = 'Europe/Moscow'
    description: str = Field(default='', max_length=10000)
    location: str = Field(default='', max_length=1000)
    category: Literal['meeting','work','personal','anniversary'] = 'meeting'
    availability: Literal['busy','free','away'] = 'busy'
    recurrence: Literal['none','daily','weekly','monthly'] = 'none'
    count: int = Field(default=1, ge=1, le=366)
    participants: list[int] = Field(default_factory=list, max_length=100)
    coorganizers: list[int] = Field(default_factory=list,max_length=20)
    editors: list[int] = Field(default_factory=list,max_length=100)
    tags: list[str] = Field(default_factory=list,max_length=20)
    links: list[DocumentLink] = Field(default_factory=list,max_length=10)
    attachment_ids: list[UUID] = Field(default_factory=list,max_length=10)
    task_assignee_id: int | None = Field(default=None,gt=0)
    task_reviewer_id: int | None = Field(default=None,gt=0)
    cancelled: bool = False
    all_day: bool = False
    reminder_minutes: Literal[0,5,15,30,60,1440] | None = None

    @model_validator(mode='after')
    def valid(self):
        if self.start.tzinfo is None or self.end.tzinfo is None or self.end <= self.start:
            raise ValueError('Укажите начало и окончание с часовым поясом')
        if self.end - self.start > timedelta(days=31): raise ValueError('Событие не может длиться больше 31 дня')
        try: ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError,ValueError): raise ValueError('Неизвестный часовой пояс')
        if self.all_day:
            zone = ZoneInfo(self.timezone)
            if any(d.astimezone(zone).time().isoformat() != '00:00:00' for d in (self.start, self.end)):
                raise ValueError('Событие на весь день должно начинаться и заканчиваться в полночь своего часового пояса')
        if len(set(self.participants)) != len(self.participants): raise ValueError('Участники повторяются')
        if not self.title.strip(): raise ValueError('Укажите тему')
        if not set(self.coorganizers+self.editors).issubset(self.participants): raise ValueError('Редакторы и соорганизаторы должны быть участниками')
        if len(set(self.coorganizers+self.editors)) != len(self.coorganizers+self.editors): raise ValueError('Роли участников повторяются')
        if len(set(self.attachment_ids)) != len(self.attachment_ids): raise ValueError('Вложения повторяются')
        if any(not t.strip() or len(t)>40 for t in self.tags): raise ValueError('Тег должен содержать от 1 до 40 символов')
        object.__setattr__(self,'tags',list(dict.fromkeys(t.strip() for t in self.tags)))
        return self

def expanded_occurrences(data, start, end):
    if data['cancelled'] or data.get('deleted'): return []
    zone = ZoneInfo(data['timezone'])
    first = datetime.fromisoformat(data['start']).astimezone(zone)
    duration = datetime.fromisoformat(data['end']).astimezone(timezone.utc) - first.astimezone(timezone.utc)
    result = []
    for i in range(data['count'] if data['recurrence'] != 'none' else 1):
        at = first
        if data['recurrence'] in ('daily','weekly'): at += timedelta(days=i*(7 if data['recurrence']=='weekly' else 1))
        elif data['recurrence']=='monthly':
            month = first.year*12+first.month-1+i; year, m = divmod(month,12)
            if first.day > monthrange(year,m+1)[1]: continue
            at = first.replace(year=year,month=m+1)
        if data.get('all_day'):
            days = (datetime.fromisoformat(data['end']).astimezone(zone).date()-first.date()).days
            finish = (at+timedelta(days=days)).astimezone(timezone.utc)
        else:
            finish = at.astimezone(timezone.utc)+duration
        exception = data.get('exceptions', {}).get(str(i))
        if exception:
            at = datetime.fromisoformat(exception['start'])
            finish = datetime.fromisoformat(exception['end'])
        if at < end and finish > start: result.append((str(i),at,finish))
    return result

def occurrences(data, start, end):
    return [(at, finish) for _, at, finish in expanded_occurrences(data, start, end)]

def busy_people(row):
    """A task block reserves its executor, while organizers can still review it."""
    if row.data.get('task_assignee_id'): return {row.data['task_assignee_id']}
    return {row.owner_id, *row.data['participants']}

def visible(row, user): return row.owner_id == user.id or user.id in row.data['participants']
def can_edit(row,user): return visible(row,user) and (row.owner_id==user.id or user.id in row.data.get('coorganizers',[])+row.data.get('editors',[]))
def can_delete(row,user): return row.owner_id==user.id or user.id in row.data.get('coorganizers',[])
def out(row, user):
    from .calendar_tasks import task_summary
    return dict(id=row.id,owner_id=row.owner_id,version=row.version,can_edit=can_edit(row,user),can_delete=can_delete(row,user),can_manage_access=row.owner_id==user.id,can_respond=user.id in row.data['participants'] and user.id!=row.owner_id,**row.data,task=task_summary(row,user))
def period(start,end):
    if start.tzinfo is None or end.tzinfo is None or end<=start or end-start>timedelta(days=93):
        raise HTTPException(422,'Период должен быть от 1 минуты до 93 дней, с часовым поясом')


class SlotSearch(Strict):
    start: datetime
    end: datetime
    participants: list[int] = Field(default_factory=list, max_length=100)
    duration_minutes: int = Field(default=60, ge=5, le=480)
    timezone: str = 'Europe/Moscow'
    work_start: int = Field(default=480, ge=0, lt=1440)
    work_end: int = Field(default=1140, gt=0, le=1440)
    weekdays: list[int] = Field(default_factory=lambda: [0,1,2,3,4], min_length=1, max_length=7)
    exclude_event_id: int | None = Field(default=None, gt=0)

    @model_validator(mode='after')
    def valid(self):
        if self.start.tzinfo is None or self.end.tzinfo is None or not timedelta(0) < self.end-self.start <= timedelta(days=14):
            raise ValueError('Выберите период до 14 дней с часовым поясом')
        if self.work_start >= self.work_end or self.duration_minutes > self.work_end-self.work_start:
            raise ValueError('Встреча должна помещаться в рабочие часы')
        if any(d not in range(7) for d in self.weekdays) or len(set(self.weekdays)) != len(self.weekdays):
            raise ValueError('Укажите неповторяющиеся дни недели от 0 до 6')
        try: ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError): raise ValueError('Неизвестный часовой пояс')
        return self


@router.post('/suggestions')
def suggestions(body: SlotSearch, db: Session=Depends(get_db), user: User=Depends(get_current_user)):
    """Suggestions are a snapshot, not a resource reservation or a guarantee."""
    ids = set(body.participants) | {user.id}
    if db.query(User).filter(User.id.in_(ids), User.is_active.is_(True)).count() != len(ids):
        raise HTTPException(422, 'Выберите действующих сотрудников')
    if body.exclude_event_id:
        excluded = db.get(Event, body.exclude_event_id)
        if not excluded or not can_edit(excluded,user):
            raise HTTPException(403, 'Исключить из проверки можно только свою встречу')
    intervals = []
    for row in db.query(Event):
        if row.id == body.exclude_event_id or row.data['availability'] == 'free': continue
        involved = busy_people(row) & ids
        # The organizer remains busy even if mistakenly included as a declining invitee.
        involved = {p for p in involved if p == row.owner_id or row.data.get('responses', {}).get(str(p)) != 'declined'}
        if involved:
            intervals.extend((a.astimezone(timezone.utc), b.astimezone(timezone.utc))
                             for a, b in occurrences(row.data, body.start, body.end))
    intervals.sort()
    zone = ZoneInfo(body.timezone)
    first = body.start.astimezone(zone).date()
    last = body.end.astimezone(zone).date()
    slots = []
    for offset in range((last-first).days+1):
        date = first+timedelta(days=offset)
        if date.weekday() not in body.weekdays: continue
        midnight = datetime(date.year,date.month,date.day,tzinfo=zone)
        for minute in range(body.work_start, body.work_end-body.duration_minutes+1, 15):
            at = midnight+timedelta(minutes=minute)
            utc = at.astimezone(timezone.utc)
            # Skip non-existent local times at a daylight-saving transition.
            if utc.astimezone(zone).replace(tzinfo=None) != at.replace(tzinfo=None): continue
            finish = utc+timedelta(minutes=body.duration_minutes)
            local_end = finish.astimezone(zone)
            if local_end.date()!=date or local_end.hour*60+local_end.minute>body.work_end: continue
            if utc < body.start or finish > body.end: continue
            if any(a < finish and b > utc for a,b in intervals): continue
            slots.append({'start': at.isoformat(), 'end': local_end.isoformat()})
            if len(slots) == 24: break
        if len(slots) == 24: break
    return {'slots': slots, 'participant_ids': sorted(ids), 'checked_at': datetime.now(timezone.utc).isoformat()}

@router.get('/people')
def people(db: Session=Depends(get_db),user: User=Depends(get_current_user)):
    from app.common.module_access import Module, AccessLevel
    return [{'id':u.id,'name':u.full_name,'can_work_tasks':u.access_level(Module.TASKS)>=AccessLevel.EDIT} for u in db.query(User).filter_by(is_active=True).order_by(User.full_name)]

@router.get('/events')
def events(start: datetime,end: datetime,query: str=Query('',max_length=200),db: Session=Depends(get_db),user: User=Depends(get_current_user)):
    period(start,end);result=[]
    for row in db.query(Event):
        if not visible(row,user) or query.casefold() not in (' '.join([row.data['title'],row.data['description'],row.data.get('location',''),*row.data.get('tags',[])])).casefold(): continue
        for occurrence_id,at,finish in expanded_occurrences(row.data,start,end):
            result.append(dict(out(row,user),occurrence_id=occurrence_id,occurrence_start=at.isoformat(),occurrence_end=finish.isoformat()))
    return sorted(result,key=lambda r:r['occurrence_start'])


@router.get('/invitations')
def invitations(db: Session=Depends(get_db), user: User=Depends(get_current_user)):
    """One upcoming occurrence per unanswered series, independent of the visible month."""
    now = datetime.now(timezone.utc)
    result = []
    for row in db.query(Event):
        if row.owner_id == user.id or user.id not in row.data['participants']: continue
        if row.data.get('responses', {}).get(str(user.id), 'pending') != 'pending': continue
        upcoming = occurrences(row.data, now, now+timedelta(days=366*32))
        if upcoming:
            at, finish = upcoming[0]
            result.append(dict(out(row,user),occurrence_start=at.isoformat(),occurrence_end=finish.isoformat()))
    return sorted(result,key=lambda r: datetime.fromisoformat(r['occurrence_start']))


def due_reminders(db, user, now):
    result = []
    for row in db.query(Event):
        lead = row.data.get('reminder_minutes')
        if lead is None or not visible(row,user): continue
        if row.owner_id != user.id and row.data.get('responses',{}).get(str(user.id)) == 'declined': continue
        for at, finish in occurrences(row.data,now,now+timedelta(days=1,seconds=1)):
            if at-timedelta(minutes=lead) <= now < finish:
                result.append(dict(out(row,user),occurrence_start=at.isoformat(),occurrence_end=finish.isoformat(),
                    reminder_key=f'{row.id}:{row.version}:{at.isoformat()}:{lead}'))
    return sorted(result,key=lambda r: datetime.fromisoformat(r['occurrence_start']))[:50]


@router.get('/reminders')
def reminders(db: Session=Depends(get_db), user: User=Depends(get_current_user)):
    # Read-only polling while the application is open; no email/push delivery claimed.
    return due_reminders(db,user,datetime.now(timezone.utc))

@router.get('/busy')
def busy(start: datetime,end: datetime,person_id: list[int]=Query(),db: Session=Depends(get_db),user: User=Depends(get_current_user)):
    period(start,end)
    if len(person_id)>100: raise HTTPException(422,'Не больше 100 участников')
    result=[]
    for row in db.query(Event):
        involved=busy_people(row) & set(person_id)
        if row.data['availability']=='free': continue
        for at,finish in occurrences(row.data,start,end):
            for person in involved:
                if person != row.owner_id and row.data.get('responses',{}).get(str(person))=='declined': continue
                result.append({'person_id':person,'start':at.isoformat(),'end':finish.isoformat()})
    return result

def write(db,user,body,id=None):
    advisory(db,'team-calendar')
    advisory(db,'calendar-request:'+str(body.request_id))
    previous=db.query(Change).filter_by(request_id=str(body.request_id)).first()
    payload=body.model_dump(mode='json')
    if previous:
        if previous.actor_id!=user.id or previous.data['request']!=payload or previous.data['route_id']!=id: raise HTTPException(409,'Запрос уже использован')
        existing=db.get(Event,previous.event_id)
        if not visible(existing,user) or existing.data.get('deleted'): raise HTTPException(404,'Запись недоступна')
        return out(existing,user)
    row=db.query(Event).filter_by(id=id).populate_existing().with_for_update().first() if id else None
    if id and (not row or not can_edit(row,user)): raise HTTPException(403,'Нет права редактировать запись')
    if row and row.data.get('deleted'): raise HTTPException(409,'Запись удалена')
    if row and row.version!=body.version: raise HTTPException(409,'Событие изменено. Обновите календарь.')
    if not id and body.version: raise HTTPException(422,'У нового события нет предыдущей версии')
    # Old open clients must not silently erase fields introduced in this release.
    if row:
        additions=('coorganizers','editors','tags','links','attachment_ids','task_assignee_id','task_reviewer_id')
        preserved={k:row.data[k] for k in additions if k not in body.model_fields_set and k in row.data}
        for name in ('coorganizers','editors'):
            if name in preserved: preserved[name]=[p for p in preserved[name] if p in body.participants]
        try: body=Input.model_validate({**body.model_dump(),**preserved})
        except ValidationError: raise HTTPException(422,'Проверьте участников и роли записи')
    if db.query(User).filter(User.id.in_(body.participants),User.is_active.is_(True)).count()!=len(body.participants): raise HTTPException(422,'Участник не найден')
    if row and row.owner_id!=user.id:
        for name in ('participants','coorganizers','editors'):
            if set(getattr(body,name))!=set(row.data.get(name,[])): raise HTTPException(403,'Права и участников изменяет организатор')
        for name in ('task_assignee_id','task_reviewer_id'):
            if getattr(body,name)!=row.data.get(name): raise HTTPException(403,'Исполнителя и проверяющего изменяет организатор')
        if body.cancelled != row.data['cancelled'] and not can_delete(row,user): raise HTTPException(403,'Редактор не может удалять запись')
    before=row.data if row else None
    data=body.model_dump(mode='json',exclude={'request_id','version'})
    if before and before.get('exceptions'):
        if any(data[k] != before[k] for k in ('recurrence', 'count', 'timezone', 'all_day')):
            raise HTTPException(409, 'В серии есть перенесённые встречи. Изменяйте их отдельно; для другого правила повторения создайте новую серию.')
        delta = body.start - datetime.fromisoformat(before['start'])
        data['exceptions'] = {key: {field: (datetime.fromisoformat(value[field]) + delta).isoformat() for field in ('start','end')} for key, value in before['exceptions'].items()}
    scheduling=('title','start','end','timezone','location','recurrence','count','all_day')
    changed=before is None or any(before.get(k)!=data.get(k) for k in scheduling)
    data['responses']={str(p):'pending' if changed else before.get('responses',{}).get(str(p),'pending') for p in body.participants}
    if row: row.data=data;row.version+=1
    else: row=Event(owner_id=user.id,data=data);db.add(row)
    db.flush()
    from .calendar_files import bind_files
    data={**data,'attachments':bind_files(db,user,row,data['attachment_ids'])}
    from .calendar_tasks import sync_task
    data=sync_task(db,user,row,data,before)
    row.data=data
    db.add(Change(event_id=row.id,actor_id=user.id,request_id=str(body.request_id),data={'route_id':id,'request':payload,'before':before,'after':data,'at':datetime.now(timezone.utc).isoformat()}))
    db.commit();return out(row,user)

@router.post('/events',status_code=201)
def create(body:Input,db:Session=Depends(get_db),user:User=Depends(get_current_user)): return write(db,user,body)
@router.patch('/events/{id}')
def update(id:int,body:Input,db:Session=Depends(get_db),user:User=Depends(get_current_user)): return write(db,user,body,id)

class MoveInput(Strict):
    request_id: UUID
    version: int = Field(ge=1)
    occurrence_id: str = Field(pattern=r'^\d{1,3}$')
    start: datetime
    scope: Literal['occurrence', 'series'] = 'occurrence'

    @model_validator(mode='after')
    def valid(self):
        if self.start.tzinfo is None: raise ValueError('Укажите время с часовым поясом')
        return self

@router.patch('/events/{id}/move')
def move_event(id:int,body:MoveInput,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    advisory(db,'team-calendar')
    advisory(db,'calendar-request:'+str(body.request_id))
    request={'action':'move', **body.model_dump(mode='json')}
    previous=db.query(Change).filter_by(request_id=str(body.request_id)).first()
    if previous:
        if previous.actor_id!=user.id or previous.event_id!=id or previous.data['request']!=request: raise HTTPException(409,'Запрос уже использован')
        row=db.get(Event,id)
        if not row or not visible(row,user) or row.data.get('deleted'): raise HTTPException(404,'Запись недоступна')
        return out(row,user)
    row=db.query(Event).filter_by(id=id).populate_existing().with_for_update().first()
    if not row or not can_edit(row,user): raise HTTPException(403,'Нет права редактировать запись')
    if row.version!=body.version or row.data.get('deleted') or row.data['cancelled']: raise HTTPException(409,'Событие изменено. Обновите календарь.')
    before=row.data
    occurrence=next(((a,b) for key,a,b in expanded_occurrences(before,datetime.min.replace(tzinfo=timezone.utc),datetime.max.replace(tzinfo=timezone.utc)) if key==body.occurrence_id),None)
    if not occurrence: raise HTTPException(404,'Повторение не найдено')
    at,finish=occurrence
    end=body.start+(finish.astimezone(timezone.utc)-at.astimezone(timezone.utc))
    fields={k:v for k,v in before.items() if k in Input.model_fields}
    if before.get('all_day'):
        zone=ZoneInfo(before['timezone'])
        local_start=body.start.astimezone(zone)
        if local_start.time().isoformat()!='00:00:00': raise HTTPException(422,'Событие на весь день переносится на полночь')
        end=local_start+timedelta(days=(finish.astimezone(zone).date()-at.astimezone(zone).date()).days)
    data={**before}
    if body.scope=='series' or before['recurrence']=='none':
        delta=body.start-at
        fields.update(start=(datetime.fromisoformat(before['start'])+delta).isoformat(),end=(datetime.fromisoformat(before['end'])+delta).isoformat())
        if before['recurrence']=='none': fields.update(start=body.start.isoformat(),end=end.isoformat())
        try: validated=Input.model_validate({**fields,'version':body.version,'request_id':body.request_id})
        except ValidationError as exc: raise HTTPException(422,'Проверьте время переноса') from exc
        data.update(validated.model_dump(mode='json',exclude={'request_id','version'}))
        if before.get('exceptions'):
            data['exceptions']={key:{field:(datetime.fromisoformat(value[field])+delta).isoformat() for field in ('start','end')} for key,value in before['exceptions'].items()}
    else:
        data['exceptions']={**before.get('exceptions',{}),body.occurrence_id:{'start':body.start.isoformat(),'end':end.isoformat()}}
    data['responses']={str(p):'pending' for p in data['participants']}
    from .calendar_tasks import sync_task
    data=sync_task(db,user,row,data,before)
    row.data=data;row.version+=1
    db.add(Change(event_id=id,actor_id=user.id,request_id=str(body.request_id),data={'route_id':id,'request':request,'before':before,'after':data,'at':datetime.now(timezone.utc).isoformat()}))
    db.commit()
    return out(row,user)

class Reply(Strict):
    response: Literal['accepted','tentative','declined']
    version: int

@router.post('/events/{id}/response')
def respond(id:int,body:Reply,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    row=db.query(Event).filter_by(id=id).populate_existing().with_for_update().first()
    if not row or user.id not in row.data['participants']: raise HTTPException(404,'Приглашение не найдено')
    if row.version!=body.version or row.data['cancelled']: raise HTTPException(409,'Встреча изменилась. Обновите календарь.')
    before=row.data
    row.data={**row.data,'responses':{**row.data.get('responses',{}),str(user.id):body.response}};row.version+=1
    from uuid import uuid4
    db.add(Change(event_id=id,actor_id=user.id,request_id=str(uuid4()),data={'route_id':id,'request':body.model_dump(),'before':before,'after':row.data,'at':datetime.now(timezone.utc).isoformat()}))
    db.commit();return out(row,user)


class DeleteInput(Strict):
    version: int = Field(ge=1)
    request_id: UUID

@router.delete('/events/{id}')
def delete_event(id:int,body:DeleteInput,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    advisory(db,'team-calendar')
    advisory(db,'calendar-request:'+str(body.request_id))
    request={'action':'delete','version':body.version}
    previous=db.query(Change).filter_by(request_id=str(body.request_id)).first()
    if previous:
        if previous.actor_id!=user.id or previous.event_id!=id or previous.data['request']!=request: raise HTTPException(409,'Запрос уже использован')
        return {'deleted':True,'id':id}
    row=db.query(Event).filter_by(id=id).populate_existing().with_for_update().first()
    if not row or not can_delete(row,user): raise HTTPException(403,'Нет права удалить запись')
    if row.version!=body.version or row.data.get('deleted'): raise HTTPException(409,'Запись изменилась. Обновите календарь.')
    before=row.data;row.data={**before,'deleted':True,'cancelled':True};row.version+=1
    from .calendar_tasks import archive_task
    archive_task(db,row)
    db.add(Change(event_id=id,actor_id=user.id,request_id=str(body.request_id),data={'route_id':id,'request':request,'before':before,'after':row.data,'at':datetime.now(timezone.utc).isoformat()}))
    db.commit();return {'deleted':True,'id':id}

from .calendar_files import router as files_router
router.include_router(files_router)
