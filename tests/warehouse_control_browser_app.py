"""Disposable browser acceptance data; inherited bootstrap rejects ordinary runtime use."""
import tests.costing_browser_app as t
from sqlalchemy.orm import Session
from app.common.module_access import Module, AccessLevel
from app.users.models import User, UserModuleAccess
from app.warehouse.router import app as warehouse
from app.production.router import app as production
from app.warehouse.models import WarehouseMaterial, Warehouse, MaterialCategory
from app.production.models import Production, ProductionBlock, BlockMaterial
from app.cycle.models import Cycle, CycleStatus

t.reset()
for path,app in (('warehouse',warehouse),('production',production)):
    app.dependency_overrides.update(t.app.dependency_overrides)
    t.app.mount('/api/'+path,app)
with Session(t.engine) as db:
    producer=db.get(User,3);producer.module_access=[UserModuleAccess(module=Module.PRODUCTION,level=AccessLevel.EDIT)]
    keeper=db.get(User,4);keeper.role=producer.role;keeper.full_name='Тестовый специалист склада';keeper.job_role='storekeeper'
    keeper.module_access=[UserModuleAccess(module=Module.WAREHOUSE,level=AccessLevel.EDIT)]
    cycle=Cycle(status=CycleStatus.PRODUCTION);db.add(cycle);db.flush()
    house=Production(cycle_id=cycle.id,house_index=1,name='Дом проверки склада');db.add(house);db.flush()
    material=WarehouseMaterial(title='Брус для каркаса',code='BROWSER-RAW',warehouse=Warehouse.TECHNOLOGY,category=MaterialCategory.NONE,unit='шт',quantity_in_stock=30,is_fractional=False,threshold=0)
    db.add(material);db.flush()
    for sequence,qty in ((1,4),(2,6)):
        block=ProductionBlock(production_id=house.id,name='Каркас' if sequence==1 else 'Кровля',sequence=sequence)
        db.add(block);db.flush();db.add(BlockMaterial(block_id=block.id,warehouse_material_id=material.id,inventory_number='',unit='шт',quantity_required=qty,quantity_requested=0,quantity_provided=0))
    db.commit();ids={'production_id':house.id,'material_id':material.id}

@t.app.get('/api/warehouse-test/data')
def data():return ids

if __name__=='__main__':
    import uvicorn
    uvicorn.run(t.app,host='0.0.0.0',port=8133,log_level='warning')
