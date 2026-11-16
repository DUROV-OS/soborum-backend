from uuid import uuid4
from datetime import datetime
from app.collaboration.calendar import occurrences
P='/api/projects/calendar'
def test_private_series_rsvp_and_version(api,make_user,db):
    owner,member,outsider=make_user(),make_user(),make_user()
    body=dict(request_id=str(uuid4()),title='Планёрка',start='2026-11-16T10:00:00+03:00',end='2026-11-16T11:00:00+03:00',recurrence='weekly',count=3,participants=[member.id])
    c=api(owner);r=c.post(P+'/events',json=body);assert r.status_code==201,r.text
    row=r.json();assert c.post(P+'/events',json=body).json()['id']==row['id']
    params={'start':'2026-11-16T00:00:00+03:00','end':'2026-12-01T00:00:00+03:00'}
    assert len(api(member).get(P+'/events',params=params).json())==3
    assert api(outsider).get(P+'/events',params=params).json()==[]
    busy=api(outsider).get(P+'/busy',params={**params,'person_id':member.id}).json()
    assert len(busy)==3 and set(busy[0])=={'person_id','start','end'}
    assert api(outsider).patch(P+f'/events/{row["id"]}',json={**body,'version':1,'request_id':str(uuid4())}).status_code==403
    assert api(member).post(P+f'/events/{row["id"]}/response',json={'version':1,'response':'declined'}).status_code==200
    assert api(owner).patch(P+f'/events/{row["id"]}',json={**body,'version':1,'request_id':str(uuid4())}).status_code==409
    assert api(outsider).get(P+'/busy',params={**params,'person_id':member.id}).json()==[]
    assert api(owner).patch(P+f'/events/{row["id"]}',json={**body,'version':2,'request_id':str(uuid4()),'cancelled':True}).status_code==200
    assert api(member).get(P+'/events',params=params).json()==[]
def test_timezone_and_month_end():
    data=dict(start='2026-01-31T10:00:00+03:00',end='2026-01-31T11:00:00+03:00',timezone='Europe/Moscow',recurrence='monthly',count=3,cancelled=False)
    result=occurrences(data,datetime.fromisoformat('2026-01-01T00:00:00+00:00'),datetime.fromisoformat('2026-04-01T00:00:00+00:00'))
    assert [a.month for a,b in result]==[1,3]
def test_invalid_range_and_naive_time(api,make_user):
    c=api(make_user());assert c.get(P+'/events',params={'start':'2026-01-01','end':'2026-12-01'}).status_code==422
    assert c.post(P+'/events',json=dict(request_id=str(uuid4()),title='Test',start='2026-11-16T10:00',end='2026-11-16T11:00')).status_code==422

def test_suggestions_include_organizer_and_private_busy_without_side_effects(api,make_user,db):
    from app.collaboration.calendar import Event, Change
    owner, colleague = make_user(), make_user()
    def create(who,start,end,**extra):
        return api(who).post(P+'/events',json=dict(request_id=str(uuid4()),title='Частная тема',start=start,end=end,**extra)).json()
    a=create(owner,'2026-11-16T09:00:00+03:00','2026-11-16T10:00:00+03:00')
    b=create(colleague,'2026-11-16T10:15:00+03:00','2026-11-16T11:00:00+03:00')
    body=dict(start='2026-11-16T09:00:00+03:00',end='2026-11-16T13:00:00+03:00',participants=[colleague.id],duration_minutes=60)
    r=api(owner).post(P+'/suggestions',json=body);assert r.status_code==200,r.text
    result=r.json();assert set(result['participant_ids'])=={owner.id,colleague.id}
    assert result['slots'][0]['start']=='2026-11-16T11:00:00+03:00'
    assert all(set(s)=={'start','end'} for s in result['slots'])
    assert db.query(Event).count()==2 and db.query(Change).count()==2
    assert api(owner).post(P+'/suggestions',json={**body,'exclude_event_id':b['id']}).status_code==403
    r=api(owner).post(P+'/suggestions',json={**body,'exclude_event_id':a['id']}).json()
    assert r['slots'][0]['start']=='2026-11-16T09:00:00+03:00'

def test_suggestions_declined_free_cancelled_and_recurrence(api,make_user):
    owner, colleague=make_user(),make_user()
    base=dict(request_id=str(uuid4()),title='Повторение',start='2026-11-09T09:00:00+03:00',end='2026-11-09T10:00:00+03:00',recurrence='weekly',count=3,participants=[owner.id])
    row=api(colleague).post(P+'/events',json=base).json()
    body=dict(start='2026-11-16T09:00:00+03:00',end='2026-11-16T10:00:00+03:00',participants=[],duration_minutes=60)
    assert api(owner).post(P+'/suggestions',json=body).json()['slots']==[]
    assert api(owner).post(P+f'/events/{row["id"]}/response',json={'version':1,'response':'declined'}).status_code==200
    assert len(api(owner).post(P+'/suggestions',json=body).json()['slots'])==1
    for state in ({'availability':'free'},{'cancelled':True}):
        assert api(colleague).post(P+'/events',json={**base,**state,'request_id':str(uuid4())}).status_code==201
    assert len(api(owner).post(P+'/suggestions',json=body).json()['slots'])==1

def test_suggestions_timezone_workday_and_validation(api,make_user):
    user=make_user();c=api(user)
    body=dict(start='2026-11-16T00:00:00Z',end='2026-11-17T00:00:00Z',timezone='Asia/Yekaterinburg',duration_minutes=60)
    r=c.post(P+'/suggestions',json=body);assert r.status_code==200,r.text
    assert r.json()['slots'][0]['start']=='2026-11-16T09:00:00+05:00'
    for extra in ({'work_start':1000,'work_end':900},{'duration_minutes':0},{'timezone':'Unknown/Zone'}, {'weekdays':[7]}, {'participants':[999999]}, {'end':'2027-01-01T00:00:00Z'}):
        assert c.post(P+'/suggestions',json={**body,**extra}).status_code==422
    weekend={**body,'start':'2026-11-21T00:00:00Z','end':'2026-11-22T00:00:00Z'}
    assert c.post(P+'/suggestions',json=weekend).json()['slots']==[]
    assert c.post(P+'/suggestions',json={**weekend,'weekdays':[5]}).json()['slots']

def test_suggestions_skip_nonexistent_dst_times(api,make_user):
    c=api(make_user())
    r=c.post(P+'/suggestions',json=dict(start='2026-03-29T00:00:00Z',end='2026-03-29T05:00:00Z',timezone='Europe/Berlin',weekdays=[6],work_start=120,work_end=240,duration_minutes=30))
    assert r.status_code==200,r.text
    assert all(datetime.fromisoformat(s['start']).hour>=3 for s in r.json()['slots'])

def test_calendar_preferences_private_versioned_and_dashboard_independent(api,make_user,db):
    owner, other = make_user(), make_user()
    client=api(owner)
    prefs=client.get(P+'/preferences').json()
    assert prefs['version']==0
    prefs.update(timezone='Asia/Yekaterinburg',work_start=600)
    result=client.put(P+'/preferences',json=prefs)
    assert result.status_code==200,result.text
    saved=result.json();assert saved['version']==1
    assert client.get('/api/auth/me/workspace').json() is None
    assert client.put(P+'/preferences',json=prefs).status_code==409
    assert api(other).get(P+'/preferences').json()['timezone']=='Europe/Moscow'
    workspace={'view':'general','blocks':['personal_tasks'],'sections':['tasks']}
    client=api(owner)
    assert client.put('/api/auth/me/workspace',json=workspace).json()==workspace
    assert client.get(P+'/preferences').json()==saved
    saved['categories']['meeting']['label']='Планёрки'
    saved['categories']['meeting']['color']='#123456'
    assert client.put(P+'/preferences',json=saved).status_code==200
    assert client.get('/api/auth/me/workspace').json()==workspace

def test_calendar_preferences_validation(api,make_user):
    client=api(make_user());prefs=client.get(P+'/preferences').json()
    for change in ({'timezone':'Unknown/Zone'},{'weekdays':[]},{'weekdays':[1,1]}, {'work_start':1100,'work_end':1000},{'categories':{}},{'user_id':999}):
        assert client.put(P+'/preferences',json={**prefs,**change}).status_code==422
    prefs['categories']['meeting']['color']='javascript:alert(1)'
    assert client.put(P+'/preferences',json=prefs).status_code==422
