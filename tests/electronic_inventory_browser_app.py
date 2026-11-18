"""Electronic counting on a disposable mobile test server."""
import tests.warehouse_control_browser_app as w
from sqlalchemy.orm import Session
from app.warehouse.models import WarehouseMaterial,Warehouse,MaterialCategory

t=w.t
with Session(t.engine) as db:
    zero=WarehouseMaterial(title='Крепёж',code='MOBILE-ZERO',warehouse=Warehouse.TECHNOLOGY,category=MaterialCategory.NONE,unit='шт',quantity_in_stock=0,is_fractional=False)
    fractional=WarehouseMaterial(title='Утеплитель',code='MOBILE-FRACTION',warehouse=Warehouse.TECHNOLOGY,category=MaterialCategory.NONE,unit='м³',quantity_in_stock=2.5,is_fractional=True)
    db.add_all([zero,fractional]);db.commit()
    ids={**w.ids,'zero_id':zero.id,'fractional_id':fractional.id}

@t.app.get('/api/electronic-test/data')
def data():return ids

if __name__=='__main__':
    import uvicorn
    uvicorn.run(t.app,host='0.0.0.0',port=8133,log_level='warning')
