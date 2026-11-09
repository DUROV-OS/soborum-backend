"""Historical issue and count scenarios in the explicitly isolated browser runtime."""
import tests.warehouse_control_browser_app as w
from sqlalchemy.orm import Session
from app.warehouse import service as s,documents as d
from app.warehouse.models import StockMovement,WarehouseMaterial,Warehouse
from app.production.models import Production
from app.users.models import User
from uuid import uuid4

t=w.t
with Session(t.engine) as db:
    keeper=db.get(User,4)
    op=s.issue_by_techcard(db,db.get(Production,w.ids['production_id']),keeper,received_by='Historical browser recipient',lines=[(w.ids['material_id'],6)])
    db.commit()
    movement=db.query(StockMovement).filter_by(operation_id=op.id).one()
    doc=d.create(d.Create(request_id=uuid4(),kind='inventory',warehouse=Warehouse.TECHNOLOGY,note='Synthetic physical count act',lines=[{'material_id':w.ids['material_id'],'quantity':'23'}]),db,keeper)
    ids={**w.ids,'movement_id':movement.id,'document_id':doc['id'],'document_number':doc['number']}

@t.app.get('/api/reconciliation-test/data')
def data():return ids

if __name__=='__main__':
    import uvicorn
    uvicorn.run(t.app,host='0.0.0.0',port=8133,log_level='warning')
