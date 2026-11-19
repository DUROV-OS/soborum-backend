"""In-memory calendar API for browser acceptance; no production settings."""
import os
assert os.environ.get('CALENDAR_BROWSER_TEST')=='1'
import tests.conftest
from uuid import uuid4
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from app.db.base import Base
from app.db.session import get_db
from app.core.deps import get_current_user
from app.main import app
from app.users.models import User,UserRole,UserModuleAccess
from app.common.module_access import Module,AccessLevel
from app.accounting.seed import ensure_organizations_seed
from app.collaboration.calendar import Event
engine=create_engine('sqlite://',connect_args={'check_same_thread':False},poolclass=StaticPool)
Base.metadata.create_all(engine)
with Session(engine) as db:
    ensure_organizations_seed(db)
    for i,name in enumerate(('Тестовый руководитель','Исполнитель','Проверяющий')):
        db.add(User(email=f'calendar-{i}@example.invalid',full_name=name,hashed_password='not-a-password',is_active=True,role=UserRole.ADMIN if i==0 else UserRole.WORKER,module_access=[UserModuleAccess(module=Module.TASKS,level=AccessLevel.EDIT)]))
    db.commit()
    for name,start,end in [('Ночное событие','07:00','07:30'),('Пограничное событие','20:30','22:00')]:
        db.add(Event(owner_id=1,data={'title':name,'start':f'2026-11-19T{start}:00+03:00','end':f'2026-11-19T{end}:00+03:00','category':'meeting','timezone':'Europe/Moscow','description':'','location':'','availability':'busy','recurrence':'none','count':1,'participants':[],'responses':{},'cancelled':False}))
    db.commit()
def session():
    with Session(engine) as db:yield db
def actor():
    with Session(engine) as db:
        user=db.get(User,1);list(user.module_access);db.expunge(user);return user
for subapp in [app]+[r.app for r in app.routes if hasattr(r,'app') and hasattr(r.app,'dependency_overrides')]:
    subapp.dependency_overrides[get_db]=session
    subapp.dependency_overrides[get_current_user]=actor
if __name__=='__main__':
    import uvicorn
    uvicorn.run(app,host='127.0.0.1',port=8155,lifespan='off',log_level='warning')
