"""Opt-in disposable browser fixture; no production configuration or data."""
import os
if os.environ.get('CATALOG_NAVIGATION_BROWSER_TEST') != '1':
    raise RuntimeError('Explicit isolated browser-test flag required')
os.environ['HOUSE_LIBRARY_BROWSER_TEST']='1'
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tests.production_library_browser_app import app, engine, database, user
from sqlalchemy.orm import Session
from app.core.deps import get_current_user
from app.db.session import get_db
from app.house_models.models import HouseModelCard,HouseModelKind,HouseModelConfirmation
from app.costing.models import CostingItem,CostingMaterial
from app.costing.schemas import ItemData
from app.costing.router import app as costing
from app.house_models.router import app as houses
from app.moysklad.router import app as recipes
from datetime import date
for target,path in [(costing,'/api/costing'),(houses,'/api/house-models'),(recipes,'/api/moysklad')]:
    target.dependency_overrides[get_db]=database
    target.dependency_overrides[get_current_user]=user
    app.mount(path,target)
with Session(engine) as db:
    model=HouseModelCard(key='barn-dh21',title='Барн DH 21',kind=HouseModelKind.CATALOG,series='barn',
        area_footprint_m2=21.4,area_total_m2=16,price_site_rub=1800000,
        confirmation=HouseModelConfirmation.PARTIAL,confirmation_label='Тестовая карточка',source_note_path='isolated-fixture',
        characteristics_md='Высота потолка — 2,7 м. Один жилой модуль.',planning_md='Кухня-гостиная, спальня и санузел.',
        modules_md='Модуль 7,13 × 3 м.',configurations_md='Базовая и расширенная комплектация.')
    mat=CostingMaterial(name='Тестовая доска',unit='м3',category='Древесина',price=5000,price_source='Тестовый прайс',effective_on=date(2026,11,5))
    db.add_all([model,mat]);db.flush()
    data=ItemData(house_model_key=model.key,source_recipe_id=1,source_recipe_version=1,
        code='QA-21',category='Дома',model='Барн DH 21',mode='unit',area='21.4',length='7.13',width='3',
        lines=[{'material_id':mat.id,'quantity':'3.5'}],retail_price='1800000',
        expenses={k:'0' for k in ('labor','transport','subcontract','overhead','commercial','advertising','taxes','reserve')}).model_dump(mode='json')
    db.add(CostingItem(title='Барн DH 21 · Базовая комплектация',kind='template',content=data))
    db.commit()
if __name__=='__main__':
    import uvicorn
    uvicorn.run(app,host='127.0.0.1',port=8139,log_level='warning')
