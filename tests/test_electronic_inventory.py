from pathlib import Path
from uuid import uuid4
import pytest
from app.common.module_access import Module,AccessLevel
from app.warehouse.models import StockMovement,StockMovementReason,StockDocument
from app.warehouse.control_models import WarehouseInventoryCount as Count,WarehouseInventoryAct as Act
from tests.test_stock_documents import setup,create
from tests.warehouse_control_helpers import evidence
P='/api/warehouse/inventory-counts/'


def count(client,doc,material,quantity='8',**extra):
    body={'request_id':str(uuid4()),'version':doc['version'],'material_id':material.id,'quantity':quantity,**extra}
    result=client.post(P+str(doc['id']),json=body)
    assert result.status_code==200,result.text
    return result.json()['document'],body


def confirm(client,doc):
    body={'request_id':str(uuid4()),'version':doc['version'],'confirmed':True}
    result=client.post(P+str(doc['id'])+'/confirm',json=body)
    assert result.status_code==201,result.text
    return result.json(),body


def test_electronic_act_without_upload_and_post_exactly_once(db,api,make_user):
    client,owner,m,_=setup(db,api,make_user)
    doc,_=create(client,m,kind='inventory',quantity=None)
    doc,body=count(client,doc,m,comment='Physically counted')
    assert client.post(P+str(doc['id']),json=body).status_code==200
    assert db.query(Count).count()==1 and not db.query(StockMovement).count()
    act,approval=confirm(client,doc)
    assert act['mode']=='electronic' and act['current']
    assert client.post(P+str(doc['id'])+'/confirm',json=approval).json()['id']==act['id']
    data=client.get('/api/warehouse/control/documents/'+str(act['proof']['file_id']))
    assert data.status_code==200 and 'Электронный акт инвентаризации' in data.text and 'Physically counted' in data.text
    post={'version':doc['version'],'request_id':str(uuid4()),'act_id':act['id'],'confirmed':True}
    for _ in range(2):assert client.post('/api/warehouse/documents/'+str(doc['id'])+'/post',json=post).status_code==200
    db.refresh(m);assert m.quantity_in_stock==8 and db.query(StockMovement).count()==1
    history=client.get('/api/warehouse/inventory-evidence/'+str(doc['id'])).json()
    assert history[0]['mode']=='electronic' and history[0]['current']


def test_draft_quantities_are_not_electronic_observations(db,api,make_user):
    client,owner,m,_=setup(db,api,make_user)
    doc,_=create(client,m,kind='inventory',quantity='8')
    assert client.post(P+str(doc['id'])+'/confirm',json={'request_id':str(uuid4()),'version':doc['version'],'confirmed':True}).status_code==422
    assert db.query(Act).count()==0


def test_count_permissions_and_personal_actor(db,api,make_user):
    client,owner,m,_=setup(db,api,make_user)
    doc,_=create(client,m,kind='inventory',quantity=None)
    worker=make_user(Module.WAREHOUSE,level=AccessLevel.EDIT);body={'request_id':str(uuid4()),'version':doc['version'],'material_id':m.id,'quantity':'8'}
    assert api(make_user(Module.WAREHOUSE,level=AccessLevel.VIEW)).post(P+str(doc['id']),json=body).status_code==403
    doc=api(worker).post(P+str(doc['id']),json=body).json()['document']
    assert doc['lines'][0]['counted_by']==worker.full_name
    act,_=confirm(api(worker),doc)
    assert api(worker).post('/api/warehouse/documents/'+str(doc['id'])+'/post',json={'request_id':str(uuid4()),'version':doc['version'],'act_id':act['id'],'confirmed':True}).status_code==403
    assert api(make_user(Module.CLIENTS)).get(P+str(doc['id'])).status_code==403


def test_count_history_correction_and_foreign_idempotency(db,api,make_user):
    client,owner,m,_=setup(db,api,make_user);doc,_=create(client,m,kind='inventory',quantity=None)
    doc,first=count(client,doc,m);act,_=confirm(client,doc)
    doc,second=count(client,doc,m,'9')
    assert db.query(Count).count()==2 and db.query(Count).order_by(Count.id.desc()).first().content['previous_count_id']
    assert client.post(P+str(doc['id']),json={**second,'quantity':'7'}).status_code==409
    other,_=create(client,m,kind='inventory',quantity=None)
    assert client.post(P+str(other['id']),json=second).status_code==409
    assert not client.get('/api/warehouse/inventory-evidence/'+str(doc['id'])).json()[0]['current']
    assert len(client.get(P+str(doc['id'])).json())==2


@pytest.mark.parametrize('quantity',['-1','NaN','1.0000001','1000000000000'])
def test_invalid_count_quantities(db,api,make_user,quantity):
    client,owner,m,_=setup(db,api,make_user);doc,_=create(client,m,kind='inventory',quantity=None)
    response=client.post(P+str(doc['id']),json={'request_id':str(uuid4()),'version':doc['version'],'material_id':m.id,'quantity':quantity})
    assert response.status_code==422 and not db.query(Count).count()


def test_stale_version_fractional_and_foreign_material(db,api,make_user):
    client,owner,m,other=setup(db,api,make_user);m.is_fractional=False;db.commit()
    doc,_=create(client,m,kind='inventory',quantity=None)
    body={'request_id':str(uuid4()),'version':doc['version'],'material_id':m.id,'quantity':'0.5'}
    assert client.post(P+str(doc['id']),json=body).status_code==422
    assert client.post(P+str(doc['id']),json={**body,'material_id':other.id,'quantity':'0'}).status_code==422
    doc,_=count(client,doc,m,'0')
    assert client.post(P+str(doc['id']),json={**body,'quantity':'2'}).status_code==409


def test_each_physical_recount_takes_fresh_baseline_but_later_movements_block(db,api,make_user):
    from app.warehouse.service import log_movement
    client,owner,m,_=setup(db,api,make_user);doc,_=create(client,m,kind='inventory',quantity=None)
    doc,_=count(client,doc,m,'8')
    m.quantity_in_stock=11;log_movement(db,m,1,StockMovementReason.RECEIPT,owner);db.commit()
    approval={'request_id':str(uuid4()),'version':doc['version'],'confirmed':True}
    assert client.post(P+str(doc['id'])+'/confirm',json=approval).status_code==409
    doc,_=count(client,doc,m,'9')
    assert doc['lines'][0]['expected_quantity']=='11.0' or float(doc['lines'][0]['expected_quantity'])==11
    act,_=confirm(client,doc)
    for delta in (1,-1):m.quantity_in_stock+=delta;log_movement(db,m,delta,StockMovementReason.RECEIPT if delta>0 else StockMovementReason.WRITE_OFF,owner)
    db.commit()
    assert client.post('/api/warehouse/documents/'+str(doc['id'])+'/post',json={'request_id':str(uuid4()),'version':doc['version'],'act_id':act['id'],'confirmed':True}).status_code==409


def test_photo_integrity_html_escaping_and_note_change(db,api,make_user):
    client,owner,m,_=setup(db,api,make_user);m.title='<script>alert(1)</script>';db.commit()
    doc,_=create(client,m,kind='inventory',quantity=None);photo=evidence(db,owner);db.commit()
    doc,_=count(client,doc,m,file_id=photo.id,comment='<img src=x onerror=alert(1)>')
    act,_=confirm(client,doc);html=client.get('/api/warehouse/control/documents/'+str(act['proof']['file_id'])).text
    assert '<script>' not in html and '&lt;script&gt;' in html and '<img' not in html
    updated=client.patch('/api/warehouse/documents/'+str(doc['id']),json={'version':doc['version'],'note':'Corrected note','lines':[{'material_id':m.id,'quantity':'8'}]}).json()
    assert updated['lines'][0]['count_id']==doc['lines'][0]['count_id']
    assert not client.get('/api/warehouse/inventory-evidence/'+str(doc['id'])).json()[0]['current']
    act,_=confirm(client,updated)
    Path(photo.path_on_disk).write_bytes(b'damaged photo')
    assert not client.get('/api/warehouse/inventory-evidence/'+str(doc['id'])).json()[0]['intact']
    assert client.post('/api/warehouse/documents/'+str(doc['id'])+'/post',json={'request_id':str(uuid4()),'version':updated['version'],'act_id':act['id'],'confirmed':True}).status_code==409


def test_recount_reset_cannot_reuse_old_observations(db,api,make_user):
    client,owner,m,_=setup(db,api,make_user);doc,_=create(client,m,kind='inventory',quantity=None);doc,_=count(client,doc,m)
    doc=client.post('/api/warehouse/documents/'+str(doc['id'])+'/recount',json={'version':doc['version']}).json()
    assert not doc['lines'][0].get('count_id') and doc['lines'][0]['quantity'] is None
    assert len(client.get(P+str(doc['id'])).json())==1
    assert client.post(P+str(doc['id'])+'/confirm',json={'request_id':str(uuid4()),'version':doc['version'],'confirmed':True}).status_code==422
