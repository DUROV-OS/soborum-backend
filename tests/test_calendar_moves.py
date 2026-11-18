from uuid import uuid4
from app.collaboration.calendar import Change, Event
from app.common.module_access import Module
from app.tasks.models import Task

P='/api/projects/calendar'
PERIOD={'start':'2026-11-01T00:00:00Z','end':'2026-12-01T00:00:00Z'}

def create(client, **extra):
    response=client.post(P+'/events',json=dict(request_id=str(uuid4()),title='Планёрка',start='2026-11-16T10:00:00+03:00',end='2026-11-16T11:00:00+03:00',**extra))
    assert response.status_code==201,response.text
    return response.json()

def move(client,row,**extra):
    body={'request_id':str(uuid4()),'version':row['version'],'occurrence_id':'0','start':'2026-11-17T12:15:00+03:00','scope':'occurrence',**extra}
    return client.patch(P+f'/events/{row["id"]}/move',json=body)

def test_single_move_duration_permissions_version_and_retry(api,make_user,db):
    owner,guest=make_user(),make_user()
    client=api(owner);row=create(client,participants=[guest.id])
    assert move(api(guest),row).status_code==403
    client=api(owner)
    request=str(uuid4())
    response=move(client,row,request_id=request)
    assert response.status_code==200,response.text
    assert response.json()['end']=='2026-11-17T13:15:00+03:00'
    assert move(client,row,request_id=request).status_code==200
    assert db.query(Change).filter_by(event_id=row['id']).count()==2
    assert move(client,row).status_code==409
    assert move(client,response.json(),start='2026-11-17T12:00:00').status_code==422

def test_recurrence_exception_busy_list_preserve_on_edit_and_series_move(api,make_user,db):
    owner=make_user();client=api(owner)
    row=create(client,recurrence='weekly',count=3)
    moved=move(client,row,occurrence_id='1',start='2026-11-24T12:00:00+03:00')
    assert moved.status_code==200,moved.text
    rows=client.get(P+'/events',params=PERIOD).json()
    assert len(rows)==3
    assert next(r for r in rows if r['occurrence_id']=='1')['occurrence_start']=='2026-11-24T12:00:00+03:00'
    # A moved occurrence is found by its new date, including in availability.
    narrow={'start':'2026-11-24T08:00:00Z','end':'2026-11-24T11:00:00Z'}
    assert len(client.get(P+'/events',params=narrow).json())==1
    busy=client.get(P+'/busy',params={**narrow,'person_id':owner.id})
    assert busy.status_code==200 and len(busy.json())==1
    record=db.get(Event,row['id'])
    from app.collaboration.calendar import Input
    body={k:v for k,v in record.data.items() if k in Input.model_fields}
    body.update(title='Новая тема',version=2,request_id=str(uuid4()))
    edit=client.patch(P+f'/events/{row["id"]}',json=body)
    assert edit.status_code==200,edit.text
    assert edit.json()['exceptions']==moved.json()['exceptions']
    result=move(client,edit.json(),scope='series',start='2026-11-17T10:00:00+03:00')
    assert result.status_code==200,result.text
    assert result.json()['exceptions']['1']['start']=='2026-11-25T12:00:00+03:00'
    assert move(client,result.json(),occurrence_id='99').status_code==404

def test_task_move_updates_native_deadline(api,make_user,db):
    owner=make_user(Module.TASKS);assignee=make_user(Module.TASKS)
    client=api(owner)
    row=create(client,category='work',task_assignee_id=assignee.id,task_reviewer_id=owner.id,participants=[assignee.id])
    result=move(client,row)
    assert result.status_code==200,result.text
    task=db.get(Task,result.json()['task']['id'])
    assert task.deadline.isoformat().startswith('2026-11-17T10:15:00')
    assert task.link_meta['calendar_start']=='2026-11-17T12:15:00+03:00'

def test_working_hours_default_and_saved_categories_isolated(api,make_user):
    owner,other=make_user(),make_user()
    client=api(owner);prefs=client.get(P+'/preferences').json()
    assert (prefs['work_start'],prefs['work_end'])==(480,1140)
    prefs['categories']['meeting']['color']='#AABBCC'
    prefs['work_start']=510
    assert client.put(P+'/preferences',json=prefs).status_code==200
    loaded=client.get(P+'/preferences').json()
    assert loaded['work_start']==510 and loaded['categories']['meeting']['color']=='#AABBCC'
    assert api(other).get(P+'/preferences').json()['work_start']==480

def test_all_day_move_keeps_midnight(api,make_user):
    client=api(make_user())
    response=client.post(P+'/events',json={'request_id':str(uuid4()),'title':'День рождения','start':'2026-11-16T00:00:00+03:00','end':'2026-11-17T00:00:00+03:00','all_day':True})
    row=response.json()
    assert move(client,row).status_code==422
    moved=move(client,row,start='2026-11-18T00:00:00+03:00')
    assert moved.status_code==200,moved.text
    assert moved.json()['end']=='2026-11-19T00:00:00+03:00'
