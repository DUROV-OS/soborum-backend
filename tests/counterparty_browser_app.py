"""Isolated in-memory API for browser tests. Never imports a production database."""
import os
assert os.environ.get('COUNTERPARTY_BROWSER_TEST')=='1'
import tests.conftest  # sets isolated settings before application imports
from uuid import uuid4
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from app.db.base import Base
from app.db.session import get_db
from app.core.deps import get_current_user
from app.main import app
from app.users.models import User, UserRole
from app.accounting.models import Counterparty, CounterpartyKind
from app.warehouse.models import Supplier
from app.moysklad.models import SourceLink, SourceRecord
from app.accounting.seed import ensure_organizations_seed

engine=create_engine('sqlite://',connect_args={'check_same_thread':False},poolclass=StaticPool)
Base.metadata.create_all(engine)
with Session(engine) as db:
    ensure_organizations_seed(db)
    user=User(full_name='Тестовый бухгалтер',email='counterparty@example.invalid',hashed_password='not-a-password',is_active=True,role=UserRole.ADMIN)
    supplier=Supplier(name='Архивный поставщик',categories=[],contacts=[])
    db.add_all([user,supplier]);db.flush()
    cp=Counterparty(name=supplier.name,kind=CounterpartyKind.SUPPLIER,supplier_id=supplier.id)
    db.add(cp);db.flush();cp_id=cp.id;user_id=user.id
    sid=str(uuid4());db.add(SourceLink(source_key='counterparty/'+sid,target_type='counterparties',target_id=cp.id))
    db.add(SourceRecord(entity_type='counterparty',source_id=sid,title='Источник',search_text='',payload={'id':sid,'name':'Не заменять название','companyType':'legal','inn':'7736050003','legalAddress':'Москва, адрес из источника','accounts':{'rows':[]},'contactpersons':{'rows':[]}}))
    db.add(SourceRecord(entity_type='facturein',source_id=str(uuid4()),title='СФ-42',search_text='',payload={'agent':{'id':sid},'moment':'2026-11-18 11:00:00','name':'СФ-42','sum':150000,'applicable':True}))
    db.commit()

def session():
    with Session(engine) as db:yield db

def actor():
    with Session(engine) as db:
        user=db.get(User,user_id);db.expunge(user);return user

for subapp in [app]+[r.app for r in app.routes if hasattr(r,'app') and hasattr(r.app,'dependency_overrides')]:
    subapp.dependency_overrides[get_db]=session
    subapp.dependency_overrides[get_current_user]=actor

if __name__=='__main__':
    import uvicorn
    uvicorn.run(app,host='127.0.0.1',port=8154,lifespan='off',log_level='warning')
