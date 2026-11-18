from datetime import datetime, timezone
from uuid import uuid4
from app.collaboration.calendar import due_reminders, occurrences

P = '/api/projects/calendar'

def event(**extra):
    return dict(request_id=str(uuid4()), title='Планёрка', start='2030-11-18T10:00:00+03:00',
                end='2030-11-18T11:00:00+03:00', **extra)

def test_invitations_are_private_upcoming_and_one_per_series(api, make_user):
    owner, member, other = make_user(), make_user(), make_user()
    row=api(owner).post(P+'/events',json=event(participants=[member.id],recurrence='weekly',count=5)).json()
    assert [e['id'] for e in api(member).get(P+'/invitations').json()]==[row['id']]
    assert api(owner).get(P+'/invitations').json()==[]
    assert api(other).get(P+'/invitations').json()==[]
    assert api(member).post(P+f'/events/{row["id"]}/response',json={'version':1,'response':'accepted'}).status_code==200
    assert api(member).get(P+'/invitations').json()==[]

def test_reminder_boundaries_privacy_decline_and_cancel(api,make_user,db):
    owner, member, other = make_user(), make_user(), make_user()
    body=event(participants=[member.id],reminder_minutes=15)
    row=api(owner).post(P+'/events',json=body).json()
    at=lambda s: datetime.fromisoformat('2030-11-18T'+s+'+03:00')
    assert due_reminders(db,owner,at('09:44:59'))==[]
    reminders=due_reminders(db,owner,at('09:45:00'))
    assert len(reminders)==1 and reminders[0]['id']==row['id']
    assert len(due_reminders(db,member,at('09:45:00')))==1
    assert due_reminders(db,other,at('09:45:00'))==[]
    assert due_reminders(db,owner,at('11:00:00'))==[]
    assert api(member).post(P+f'/events/{row["id"]}/response',json={'version':1,'response':'declined'}).status_code==200
    assert due_reminders(db,member,at('09:45:00'))==[]
    assert api(owner).patch(P+f'/events/{row["id"]}',json={**body,'version':2,'request_id':str(uuid4()),'cancelled':True}).status_code==200
    assert due_reminders(db,owner,at('09:45:00'))==[]

def test_all_day_keeps_midnight_across_dst_and_validates(api,make_user):
    body=event(all_day=True,timezone='Europe/Berlin',recurrence='daily',count=3)
    body.update(start='2026-03-28T00:00:00+01:00',end='2026-03-29T00:00:00+01:00')
    client=api(make_user());r=client.post(P+'/events',json=body);assert r.status_code==201,r.text
    result=occurrences(r.json(),datetime(2026,3,28,tzinfo=timezone.utc),datetime(2026,4,1,tzinfo=timezone.utc))
    from zoneinfo import ZoneInfo
    assert all(a.hour==0 and b.astimezone(ZoneInfo('Europe/Berlin')).hour==0 for a,b in result)
    assert any((b-a.astimezone(timezone.utc)).total_seconds()==23*3600 for a,b in result)
    for extra in ({'start':'2026-03-28T10:00:00+01:00'},{'reminder_minutes':-1},{'reminder_minutes':999}):
        assert client.post(P+'/events',json={**body,**extra,'request_id':str(uuid4())}).status_code==422

def test_old_events_without_optional_fields_and_zero_reminder(api,make_user,db):
    from app.collaboration.calendar import Event
    user=make_user();body=event(reminder_minutes=0)
    row=api(user).post(P+'/events',json=body).json()
    at=datetime.fromisoformat(body['start'])
    assert len(due_reminders(db,user,at))==1
    record=db.get(Event,row['id']);data=dict(record.data);data.pop('reminder_minutes');data.pop('all_day');record.data=data;db.commit()
    assert due_reminders(db,user,at)==[]
    assert api(user).get(P+'/reminders').status_code==200
