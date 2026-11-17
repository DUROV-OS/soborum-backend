from uuid import uuid4
from datetime import datetime,timezone
import pytest
from app.common.module_access import Module
from app.collaboration.calendar import Event, Change
from app.collaboration.calendar_files import CalendarFile, MAX_FILE, file_path
from app.tasks.models import Task

P='/api/projects/calendar'
PERIOD={'start':'2026-11-16T00:00:00Z','end':'2026-11-23T00:00:00Z'}
def body(**kw):
    return dict(request_id=str(uuid4()),title='Проверка календаря',start='2026-11-17T10:00:00+03:00',end='2026-11-17T11:00:00+03:00',**kw)

@pytest.fixture(autouse=True)
def storage(tmp_path,monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings,'storage_dir',str(tmp_path))

def upload(api,user,data=b'%PDF-1.4\nfixture',name='Документ.pdf',request=None):
    return api(user).post(P+'/files',data={'request_id':request or str(uuid4())},files={'file':(name,data,'application/octet-stream')})

def test_files_private_bound_checksum_and_delete_history(api,make_user,db):
    owner,member,outsider=make_user(),make_user(),make_user()
    token=str(uuid4());r=upload(api,owner,request=token);assert r.status_code==201,r.text
    f=r.json();assert upload(api,owner,request=token).json()==f
    assert set(f)=={'id','filename','size','sha256','media_type'}
    assert api(member).get(P+'/files/'+f['id']).status_code==404
    event=api(owner).post(P+'/events',json=body(participants=[member.id],attachment_ids=[f['id']])).json()
    assert event['attachments']==[f]
    assert api(member).get(P+'/files/'+f['id']).status_code==200
    assert api(outsider).get(P+'/files/'+f['id']).status_code==404
    assert api(owner).delete(P+'/files/'+f['id']).status_code==403
    deletion={'version':1,'request_id':str(uuid4())}
    assert api(owner).request('DELETE',P+f'/events/{event["id"]}',json=deletion).status_code==200
    assert api(owner).request('DELETE',P+f'/events/{event["id"]}',json=deletion).status_code==200
    assert api(member).get(P+'/events',params=PERIOD).json()==[]
    assert api(member).get(P+'/files/'+f['id']).status_code==404
    assert db.get(Event,event['id']).data['deleted'] and db.query(Change).count()==2
    assert file_path(f['id']).is_file() and db.get(CalendarFile,f['id'])

def test_file_limits_and_format_validation(api,make_user):
    owner=make_user()
    assert upload(api,owner,b'%PDF-'+b'x'*(MAX_FILE-5)).status_code==201
    assert upload(api,owner,b'%PDF-'+b'x'*(MAX_FILE-4)).status_code==413
    for data,name in [(b'','empty.pdf'),(b'<html>','bad.pdf'),(b'%PDF-1.4','run.exe'),(b'broken','book.xlsx')]:
        assert upload(api,owner,data,name).status_code in (413,422)
    assert api(owner).post(P+'/events',json=body(attachment_ids=[str(uuid4()) for _ in range(11)])).status_code==422

def test_cannot_attach_foreign_draft_and_atomic_failure(api,make_user,db):
    owner,other=make_user(),make_user();f=upload(api,owner).json()
    assert api(other).post(P+'/events',json=body(attachment_ids=[f['id']])).status_code==403
    db.rollback()
    assert db.query(Event).count()==0 and db.get(CalendarFile,f['id']).event_id is None
    assert api(owner).delete(P+'/files/'+f['id']).status_code==204
    assert not file_path(f['id']).exists()

def test_roles_versions_old_clients_and_rsvp_preservation(api,make_user,db):
    owner,editor,co,viewer=make_user(),make_user(),make_user(),make_user()
    original=body(participants=[editor.id,co.id,viewer.id],editors=[editor.id],coorganizers=[co.id],tags=['Важное'])
    row=api(owner).post(P+'/events',json=original).json();route=P+f'/events/{row["id"]}'
    assert api(viewer).post(route+'/response',json={'response':'accepted','version':1}).status_code==200
    data={**original,'request_id':str(uuid4()),'version':2,'description':'Дополнение'}
    r=api(editor).patch(route,json=data);assert r.status_code==200,r.text
    assert r.json()['responses'][str(viewer.id)]=='accepted'
    assert api(editor).request('DELETE',route,json={'version':3,'request_id':str(uuid4())}).status_code==403
    data.update(version=3,request_id=str(uuid4()));data.pop('editors');data.pop('coorganizers');data.pop('tags')
    r=api(owner).patch(route,json=data);assert r.status_code==200,r.text
    assert r.json()['editors']==[editor.id] and r.json()['tags']==['Важное']
    assert api(co).request('DELETE',route,json={'version':4,'request_id':str(uuid4())}).status_code==200
    assert api(owner).get(P+'/events',params={**PERIOD,'query':'Важное'}).json()==[]

def test_task_is_native_private_synchronized_and_archived(api,make_user,db):
    owner,assignee,reviewer,outsider=[make_user(Module.TASKS,Module.TASKS_ALL) for _ in range(4)]
    original=body(category='work',participants=[assignee.id,reviewer.id],task_assignee_id=assignee.id,task_reviewer_id=reviewer.id)
    response=api(owner).post(P+'/events',json=original);assert response.status_code==201,response.text
    row=response.json();task_id=row['task']['id'];route=P+f'/events/{row["id"]}'
    assert api(owner).post(P+'/events',json=original).json()['task']['id']==task_id
    assert db.query(Task).count()==1
    assert api(assignee).get('/api/tasks/',params={'scope':'mine'}).json()[0]['id']==task_id
    assert api(outsider).get('/api/tasks/',params={'scope':'all'}).json()==[]
    assert api(outsider).get(f'/api/tasks/{task_id}').status_code==404
    assert api(assignee).patch(f'/api/tasks/{task_id}',json={'title':'Drift'}).status_code==409
    assert api(assignee).patch(f'/api/tasks/{task_id}/status',json={'status':'in_progress'}).status_code==200
    row=api(owner).get(P+'/events',params=PERIOD).json()[0];assert row['task']['status']=='in_progress'
    updated={**original,'title':'Обновлённая задача','end':'2026-11-17T12:00:00+03:00','version':1,'request_id':str(uuid4())}
    r=api(owner).patch(route,json=updated);assert r.status_code==200,r.text
    task=db.get(Task,task_id);assert task.title==updated['title'] and task.deadline.replace(tzinfo=timezone.utc)==datetime.fromisoformat(updated['end'])
    assert task.status.value=='in_progress'
    assert api(owner).request('DELETE',route,json={'version':2,'request_id':str(uuid4())}).status_code==200
    assert api(assignee).get('/api/tasks/',params={'scope':'mine'}).json()==[]
    assert api(assignee).patch(f'/api/tasks/{task_id}/status',json={'status':'in_review'}).status_code in (400,404)
    assert db.get(Task,task_id) is not None

def test_invalid_task_does_not_leave_calendar_or_task(api,make_user,db):
    owner=make_user();a,b=make_user(Module.TASKS),make_user(Module.TASKS)
    request=body(category='work',participants=[a.id,b.id],task_assignee_id=a.id,task_reviewer_id=b.id)
    assert api(owner).post(P+'/events',json=request).status_code==403;db.rollback()
    assert db.query(Event).count()==0 and db.query(Task).count()==0
    request.update(recurrence='weekly')
    assert api(a).post(P+'/events',json=request).status_code==422;db.rollback()
    assert db.query(Event).count()==0

@pytest.mark.parametrize('change',[{'editors':[999]},{'coorganizers':[1],'editors':[1],'participants':[1]},{'tags':['  ']},{'links':[{'label':'Bad','url':'javascript:alert(1)'}]}])
def test_input_validation(api,make_user,change):
    assert api(make_user()).post(P+'/events',json=body(**change)).status_code==422

def test_private_templates_preserved_by_old_settings_client(api,make_user):
    owner,other=make_user(),make_user()
    prefs=api(owner).get(P+'/preferences').json()
    prefs['templates']=[{'id':str(uuid4()),'title':'Еженедельная планёрка','duration_minutes':60,'recurrence':'weekly'}]
    saved=api(owner).put(P+'/preferences',json=prefs);assert saved.status_code==200,saved.text
    assert api(other).get(P+'/preferences').json()['templates']==[]
    old=saved.json();old.pop('templates');old['work_start']=600
    result=api(owner).put(P+'/preferences',json=old);assert result.status_code==200,result.text
    assert result.json()['templates'][0]['title']=='Еженедельная планёрка'
