"""Loopback-only UI acceptance with synthetic data and no external services."""
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
if os.environ.get('CONVERSIONS_BROWSER_TEST') != '1':
    raise RuntimeError('Requires CONVERSIONS_BROWSER_TEST=1')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
os.environ.update(APP_ENV='dev', DATABASE_URL='sqlite://', JWT_SECRET='synthetic-browser-only', ENABLE_DEMO_SEED='false', MAX_LIVE_UPDATES='false')
from fastapi import FastAPI, Depends
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.db import import_all_models
from app.db.base import Base
from app.db.session import get_db
from app.core.deps import get_current_user
from app.users.models import User, UserRole
from app.common.module_access import Module
from app.marketing.router import app as marketing_app
from app.clients.models import Client, ClientLeadRequest
from app.clients.service import create_client
from app.clients.schemas import ClientCreate
from app.accounting.seed import ensure_organizations_seed
from app.marketing.conversion_models import ConversionReview

temporary=TemporaryDirectory(prefix='durov-conversions-ui-')
engine=create_engine('sqlite:///'+str(Path(temporary.name)/'synthetic.db'),connect_args={'check_same_thread':False})
Base.metadata.create_all(engine)
with Session(engine) as db:
    ensure_organizations_seed(db)
    db.add(User(id=1,email='owner@example.test',full_name='Тестовый руководитель',role=UserRole.ADMIN,hashed_password='not-a-login',is_active=True))
    create_client(db,ClientCreate(full_name='Тестовый клиент',phone='89000000000',email='test@example.test'))
    db.flush()
    db.add(ClientLeadRequest(id=1,client_id=1,source='Сайт',message='Синтетическая заявка',utm={'utm_source':'yandex','utm_campaign':'715531690'},tracking={'yclid':'123456789','form_name':'quiz'},external_event_id='tilda/synthetic-one',is_repeat=False))
    db.add(ClientLeadRequest(id=2,client_id=1,source='Сайт',message='Синтетическая заявка без меток',is_repeat=True))
    db.commit()
def database():
    with Session(engine) as db: yield db
def user(db=Depends(get_db)): return db.get(User,1)
app=FastAPI()
app.dependency_overrides[get_db]=database
app.dependency_overrides[get_current_user]=user
marketing_app.dependency_overrides.update(app.dependency_overrides)
app.mount('/api/marketing',marketing_app)
@app.post('/api/auth/login')
def login(): return {'access_token':'synthetic-only','token_type':'bearer'}
@app.get('/api/auth/me')
def me():
    return {'id':1,'email':'owner@example.test','full_name':'Тестовый руководитель','role':'admin','job_role':None,'is_active':True,'module_access':{m.value:'full' for m in Module}}
@app.get('/api/auth/users')
def users(): return [me()]
@app.get('/api/notifications/')
@app.get('/api/notifications')
def notifications(): return []
@app.get('/api/notifications/unread-count')
def unread(): return {'count':0}
if __name__=='__main__':
    import uvicorn
    uvicorn.run(app,host='127.0.0.1',port=8128)
