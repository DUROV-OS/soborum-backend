from uuid import uuid4
from app.common.module_access import Module
from app.collaboration.calendar import Event
from app.tasks.models import Task

P='/api/projects/calendar'
PERIOD={'start':'2026-11-19T00:00:00Z','end':'2026-11-21T00:00:00Z'}

def test_briefing_anniversary_and_full_task_survive_reload(api,make_user,db):
    owner=make_user(admin=True);assignee=make_user(Module.TASKS);reviewer=make_user(Module.TASKS)
    c=api(owner)
    for category in ('briefing','anniversary','work'):
        body={'request_id':str(uuid4()),'title':category,'start':'2026-11-19T10:00:00+03:00','end':'2026-11-19T11:00:00+03:00','category':category}
        if category=='anniversary':body.update(all_day=True,start='2026-11-20T00:00:00+03:00',end='2026-11-21T00:00:00+03:00')
        if category=='work':body.update(task_assignee_id=assignee.id,task_reviewer_id=reviewer.id,participants=[assignee.id,reviewer.id])
        r=c.post(P+'/events',json=body);assert r.status_code==201,r.text
        assert c.post(P+'/events',json=body).json()['id']==r.json()['id']
    db.expire_all()
    rows=c.get(P+'/events',params=PERIOD).json()
    assert {r['category'] for r in rows}=={'briefing','anniversary','work'}
    assert db.query(Event).count()==3 and db.query(Task).count()==1
    assert next(r for r in rows if r['category']=='work')['task']['id']==db.query(Task).one().id


def test_upgrade_saved_preferences_and_older_client_keeps_briefing_color(api,make_user,db):
    owner=make_user();c=api(owner);prefs=c.get(P+'/preferences').json()
    prefs['categories'].pop('briefing');prefs['categories']['work']['color']='#123456';prefs['work_end']=1140
    owner.workspace_preferences={'calendar':prefs};db.commit()
    upgraded=c.get(P+'/preferences').json()
    assert upgraded['work_end']==1140 and upgraded['categories']['work']['color']=='#123456'
    upgraded['categories']['briefing']['color']='#654321'
    r=c.put(P+'/preferences',json=upgraded);assert r.status_code==200,r.text
    older=r.json();older['categories'].pop('briefing')
    r=c.put(P+'/preferences',json=older);assert r.status_code==200,r.text
    assert c.get(P+'/preferences').json()['categories']['briefing']['color']=='#654321'
