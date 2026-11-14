"""Disposable local acceptance app; starts only with an explicit test flag."""
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
if os.environ.get('HOUSE_LIBRARY_BROWSER_TEST')!='1':
    raise RuntimeError('Test flag required')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
os.environ.update(APP_ENV='dev',DATABASE_URL='sqlite://',JWT_SECRET='isolated-house-library-browser-only',ENABLE_DEMO_SEED='false')
from fastapi import FastAPI, Depends
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.db import import_all_models
from app.db.base import Base
from app.db.session import get_db
from app.core.deps import get_current_user
from app.users.models import User,UserRole
from app.common.module_access import Module
from app.production.library_router import router
from app.production.library_service import generate_cards
from app.moysklad.recipes import Recipe
temp=TemporaryDirectory(prefix='durov-house-library-test-')
engine=create_engine('sqlite:///'+str(Path(temp.name)/'test.db'),connect_args={'check_same_thread':False})
Base.metadata.create_all(engine)
with Session(engine) as db:
    person=User(id=1,email='owner@example.test',full_name='Проверка библиотеки',role=UserRole.ADMIN,hashed_password='test-only',is_active=True)
    db.add(person)
    for number,series in [(21,'Барн'),(64,'Барн'),(96,'Барн'),(27,'Флет'),(43,'Флет')]:
        db.add(Recipe(title=f'DH-{number} {series} Домокомплект',source_id=f'test-{number}-{series}',
            content={'inputs':[{'source_id':'board','title':'Доска тестовая','unit':'шт','quantity':'12'},
                               {'source_id':'insulation','title':'Утеплитель тестовый','unit':'м²','quantity':'25.5'}],
                     'outputs':[{'source_id':f'house-{number}','quantity':'1','title':'Дом','unit':'шт'}],
                     'stages':[{'name':'Сборка каркаса','standardHour':8},{'name':'Контроль качества'}],'warnings':[]}))
    db.flush();generate_cards(db,person);db.commit()
def database():
    with Session(engine) as db: yield db
def user(db=Depends(get_db)): return db.get(User,1)
app=FastAPI();production=FastAPI()
for target in [app,production]:
    target.dependency_overrides[get_db]=database
    target.dependency_overrides[get_current_user]=user
production.include_router(router);app.mount('/api/production',production)
@app.post('/api/auth/login')
def login(): return {'access_token':'synthetic-house-library-only','token_type':'bearer'}
@app.get('/api/auth/me')
def me(person=Depends(user)):
    return {'id':1,'email':person.email,'full_name':person.full_name,'role':'admin','is_active':True,'module_access':{m.value:'full' for m in Module}}
@app.get('/api/auth/users')
def users(): return []
if __name__=='__main__':
    import uvicorn
    uvicorn.run(app,host='127.0.0.1',port=8138,log_level='warning')
