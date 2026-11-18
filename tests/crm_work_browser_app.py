"""Disposable CRM acceptance server, protected by COSTING_BROWSER_TEST=1."""
import tests.costing_browser_app as t
from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session
from app.crm.router import app as crm
from app.crm.models import CRMEntity, CRMEvent
from app.crm.schemas import Entry
from app.crm import service
from app.users.models import User

crm.dependency_overrides.update(t.app.dependency_overrides)
t.app.mount('/api/crm', crm)
t.reset()
with Session(t.engine) as db:
    manager, owner = db.get(User, 2), db.get(User, 1)
    pipeline = CRMEntity(kind='pipeline', name='Продажи', payload={})
    db.add(pipeline); db.flush()
    stage = CRMEntity(kind='stage', name='Обсуждение', payload={'pipeline_id':pipeline.id,'outcome':'open'})
    db.add(stage); db.flush()
    deal = CRMEntity(kind='deal', name='Дом с договорённостью', payload={'pipeline_id':pipeline.id,'stage_id':stage.id,'responsible_user_id':manager.id})
    missing = CRMEntity(kind='deal', name='Дом без следующего шага', payload={'pipeline_id':pipeline.id,'stage_id':stage.id,'responsible_user_id':manager.id})
    db.add_all([deal,missing]);db.flush()
    db.add(CRMEvent(entity_id=deal.id, actor_id=owner.id, action='note', details={'text':'Уточнить сроки изготовления'}))
    db.commit()
    first = service.create(db,'task',Entry(name='Позвонить по проекту',data={'entity_id':deal.id,'responsible_user_id':manager.id,'due_at':(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()}),manager)
    second = service.create(db,'task',Entry(name='Отправить предложение',data={'entity_id':deal.id,'responsible_user_id':manager.id,'due_at':(datetime.now(timezone.utc)+timedelta(days=2)).isoformat()}),manager)
    service.create(db,'task',Entry(name='Уточнить адрес',data={'responsible_user_id':manager.id}),manager)
    service.create(db,'task',Entry(name='Чужая задача руководителя',data={'responsible_user_id':owner.id}),owner)
    ids={'synthetic':True,'deal':deal.id,'missing':missing.id,'first':first['id'],'second':second['id']}


@t.app.get('/api/crm-test/data')
def data():return ids


@t.app.get('/api/feedback/my')
def feedback():return []


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(t.app,host='0.0.0.0',port=8133,log_level='warning')
