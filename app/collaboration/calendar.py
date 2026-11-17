"""Internal calendar. Invitations are internal; no email delivery is implied."""
from datetime import datetime, timedelta, timezone
from calendar import monthrange
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import Field, model_validator
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
    cancelled: bool = False
    all_day: bool = False
    reminder_minutes: Literal[0,5,15,30,60,1440] | None = None

    @model_validator(mode='after')
    def valid(self):
        if self.start.tzinfo is None or self.end.tzinfo is None or self.end <= self.start:
            raise ValueError('Укажите начало и окончание с часовым поясом')
        if self.end - self.start > timedelta(days=31): raise ValueError('Событие не может длиться больше 31 дня')
        try: ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError: raise ValueError('Неизвестный часовой пояс')
        if self.all_day:
            zone = ZoneInfo(self.timezone)
            if any(d.astimezone(zone).time().isoformat() != '00:00:00' for d in (self.start, self.end)):
                raise ValueError('Событие на весь день должно начинаться и заканчиваться в полночь своего часового пояса')
        if len(set(self.participants)) != len(self.participants): raise ValueError('Участники повторяются')
        return self

def occurrences(data, start, end):
    if data['cancelled']: return []
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
        if at < end and finish > start: result.append((at,finish))
    return result

def visible(row, user): return row.owner_id == user.id or user.id in row.data['participants']
def out(row, user): return dict(id=row.id,owner_id=row.owner_id,version=row.version,can_edit=row.owner_id==user.id,**row.data)
def period(start,end):
    if start.tzinfo is None or end.tzinfo is None or end<=start or end-start>timedelta(days=93):
        raise HTTPException(422,'Период должен быть от 1 минуты до 93 дней, с часовым поясом')


class SlotSearch(Strict):
    start: datetime
    end: datetime
    participants: list[int] = Field(default_factory=list, max_length=100)
    duration_minutes: int = Field(default=60, ge=5, le=480)
    timezone: str = 'Europe/Moscow'
    work_start: int = Field(default=540, ge=0, lt=1440)
    work_end: int = Field(default=1080, gt=0, le=1440)
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
        if not excluded or excluded.owner_id != user.id:
            raise HTTPException(403, 'Исключить из проверки можно только свою встречу')
    intervals = []
    for row in db.query(Event):
        if row.id == body.exclude_event_id or row.data['availability'] == 'free': continue
        involved = ({row.owner_id} | set(row.data['participants'])) & ids
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
    return [{'id':u.id,'name':u.full_name} for u in db.query(User).filter_by(is_active=True).order_by(User.full_name)]

@router.get('/events')
def events(start: datetime,end: datetime,query: str=Query('',max_length=200),db: Session=Depends(get_db),user: User=Depends(get_current_user)):
    period(start,end);result=[]
    for row in db.query(Event):
        if not visible(row,user) or query.casefold() not in (row.data['title']+' '+row.data['description']).casefold(): continue
        for at,finish in occurrences(row.data,start,end):
            result.append(dict(out(row,user),occurrence_start=at.isoformat(),occurrence_end=finish.isoformat()))
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
        involved=set([row.owner_id,*row.data['participants']]) & set(person_id)
        if row.data['availability']=='free': continue
        for at,finish in occurrences(row.data,start,end):
            for person in involved:
                if person != row.owner_id and row.data.get('responses',{}).get(str(person))=='declined': continue
                result.append({'person_id':person,'start':at.isoformat(),'end':finish.isoformat()})
    return result

def write(db,user,body,id=None):
    advisory(db,'calendar-request:'+str(body.request_id))
    previous=db.query(Change).filter_by(request_id=str(body.request_id)).first()
    payload=body.model_dump(mode='json')
    if previous:
        if previous.actor_id!=user.id or previous.data['request']!=payload or previous.data['route_id']!=id: raise HTTPException(409,'Запрос уже использован')
        return out(db.get(Event,previous.event_id),user)
    row=db.query(Event).filter_by(id=id).populate_existing().with_for_update().first() if id else None
    if id and (not row or row.owner_id!=user.id): raise HTTPException(403,'Изменить встречу может организатор')
    if row and row.version!=body.version: raise HTTPException(409,'Событие изменено. Обновите календарь.')
    if not id and body.version: raise HTTPException(422,'У нового события нет предыдущей версии')
    if db.query(User).filter(User.id.in_(body.participants),User.is_active.is_(True)).count()!=len(body.participants): raise HTTPException(422,'Участник не найден')
    before=row.data if row else None
    data=body.model_dump(mode='json',exclude={'request_id','version'})
    data['responses']={str(p):'pending' for p in body.participants}
    if row: row.data=data;row.version+=1
    else: row=Event(owner_id=user.id,data=data);db.add(row)
    db.flush()
    db.add(Change(event_id=row.id,actor_id=user.id,request_id=str(body.request_id),data={'route_id':id,'request':payload,'before':before,'after':data,'at':datetime.now(timezone.utc).isoformat()}))
    db.commit();return out(row,user)

@router.post('/events',status_code=201)
def create(body:Input,db:Session=Depends(get_db),user:User=Depends(get_current_user)): return write(db,user,body)
@router.patch('/events/{id}')
def update(id:int,body:Input,db:Session=Depends(get_db),user:User=Depends(get_current_user)): return write(db,user,body,id)

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
