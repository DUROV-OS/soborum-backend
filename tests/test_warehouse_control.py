from decimal import Decimal as D
from pathlib import Path
from uuid import uuid4
import pytest
from fastapi import HTTPException
from app.common.module_access import Module, AccessLevel
from app.warehouse import controls as c
from app.warehouse.control_models import WarehouseControlRecord as Record
from app.warehouse.control_router import Handoff, Return
from app.warehouse.schemas import TechcardIssueCreate, MaterialRequestIssue
from app.warehouse.models import StockMovement, WarehouseOperation
from app.production.models import MaterialRequest, MaterialRequestStatus
from app.production.service import request_material
from tests.test_warehouse_issue import _house, _block, _norm, _material
from tests.warehouse_control_helpers import approved, evidence


def setup(db, make_user):
    keeper=make_user(Module.WAREHOUSE)
    production=make_user(Module.PRODUCTION)
    material=_material(db,'Control beam',30)
    house=_house(db,'Control house')
    first=_norm(db,_block(db,house,'Frame',1),material,4)
    second=_norm(db,_block(db,house,'Roof',2),material,6)
    digest,_,doc=approved(db,house.id,production)
    return keeper,production,material,house,first,second,digest,doc


def issue_body(digest,material,qty=6):
    return TechcardIssueCreate(request_id=uuid4(),source_hash=digest,received_by='Production recipient',lines=[{'warehouse_material_id':material.id,'quantity':qty}])


def return_body(doc,qty=3):
    return Return(request_id=uuid4(),quantity=qty,file_id=doc.id,locator='Page 1, item 1',reason='Unused materials',received_by='Warehouse recipient')


def test_documented_handoff_and_queue_acl(db,make_user,api):
    keeper,producer,m,p,*_=setup(db,make_user)
    result=api(keeper).get('/api/warehouse/control/queue')
    assert result.status_code==200,result.text
    item=result.json()['houses'][0]
    assert item['control']['approved'] and item['available_positions']==1
    assert api(producer).get(f'/api/warehouse/control/houses/{p.id}').json()['can_sign']
    assert api(make_user(Module.CLIENTS)).get('/api/warehouse/control/queue').status_code==403


def test_unsigned_norm_and_missing_request_key_fail_closed(db,make_user,api):
    keeper=make_user(Module.WAREHOUSE);m=_material(db,'Unsigned',10);p=_house(db);_norm(db,_block(db,p,'Frame',1),m,5);db.commit()
    client=api(keeper);body=issue_body(c.state(db,p.id)['source_hash'],m,2).model_dump(mode='json')
    assert client.post(f'/api/warehouse/techcard-issue/{p.id}',json=body).status_code==409
    assert client.post(f'/api/warehouse/techcard-issue/{p.id}',json={'received_by':'Test'}).status_code==422
    assert not db.query(StockMovement).count()


def test_admin_cannot_replace_named_production_signature(db,make_user,api):
    keeper,producer,m,p,b,b2,digest,doc=setup(db,make_user)
    body=Handoff(request_id=uuid4(),source_hash=digest,file_id=doc.id,locator='Page 1').model_dump(mode='json')
    assert api(make_user(admin=True)).post(f'/api/warehouse/control/houses/{p.id}/handoff',json=body).status_code==403


def test_norm_changes_and_corrupt_scans_invalidate_signature(db,make_user):
    keeper,producer,m,p,b,b2,digest,doc=setup(db,make_user)
    b.quantity_required=5;db.flush()
    assert not c.state(db,p.id)['approved']
    with pytest.raises(HTTPException):c.issue(db,p.id,issue_body(digest,m),keeper)
    db.rollback()
    Path(doc.path_on_disk).write_bytes(b'changed')
    assert not c.state(db,p.id)['approved']


def test_retries_do_not_issue_twice_and_key_cannot_change_payload(db,make_user):
    keeper,_,m,p,b,b2,digest,_=setup(db,make_user)
    body=issue_body(digest,m)
    first=c.issue(db,p.id,body,keeper);db.commit()
    assert c.issue(db,p.id,body,keeper).id==first.id
    assert db.query(StockMovement).count()==1 and m.quantity_in_stock==24
    with pytest.raises(HTTPException) as e:c.issue(db,p.id,body.model_copy(update={'received_by':'Other'}),keeper)
    assert e.value.status_code==409
    assert c.state(db,p.id)['approved']


def test_partial_return_restores_exact_blocks_and_allows_reissue(db,make_user):
    keeper,_,m,p,b,b2,digest,doc=setup(db,make_user)
    op=c.issue(db,p.id,issue_body(digest,m),keeper);db.commit()
    movement=db.query(StockMovement).filter_by(operation_id=op.id).one()
    returned=c.return_material(db,movement.id,return_body(doc,5),keeper);db.commit()
    assert (b.quantity_required,b.quantity_provided,b2.quantity_required,b2.quantity_provided)==(4,0,5,1)
    assert m.quantity_in_stock==29 and c.returned_quantity(db,movement.id)==5
    assert movement.delta==-6 and returned.kind.value=='receipt'
    assert c.state(db,p.id)['source_hash']==digest and c.state(db,p.id)['approved']
    c.issue(db,p.id,issue_body(digest,m,9),keeper);db.commit()
    assert m.quantity_in_stock==20 and b.quantity_provided+b2.quantity_provided==10


def test_return_retries_and_overreturn_do_not_change_stock(db,make_user):
    keeper,_,m,p,b,b2,digest,doc=setup(db,make_user)
    op=c.issue(db,p.id,issue_body(digest,m,3),keeper);db.commit()
    movement=db.query(StockMovement).filter_by(operation_id=op.id).one();body=return_body(doc,2)
    returned=c.return_material(db,movement.id,body,keeper);db.commit()
    assert c.return_material(db,movement.id,body,keeper).id==returned.id
    with pytest.raises(HTTPException):c.return_material(db,movement.id,return_body(doc,2),keeper)
    db.rollback();db.refresh(m)
    assert m.quantity_in_stock==29 and c.returned_quantity(db,movement.id)==2


def test_request_partial_retry_is_idempotent_and_return_does_not_reopen_request(db,make_user):
    keeper,producer,m,p,b,b2,digest,doc=setup(db,make_user)
    req=request_material(db,b,4,producer);db.commit()
    body=MaterialRequestIssue(request_id=uuid4(),quantity=2,source_hash=digest,received_by='Recipient')
    c.issue_request(db,req.id,body,keeper);db.commit()
    c.issue_request(db,req.id,body,keeper)
    assert req.quantity==2 and m.quantity_in_stock==28 and b.quantity_requested==2
    movement=db.query(StockMovement).one()
    c.return_material(db,movement.id,return_body(doc,1),keeper);db.commit()
    assert req.quantity==2 and b.quantity_requested==2 and b.quantity_required==1 and b.quantity_provided==1


def test_old_aggregated_issue_is_not_guessed_but_old_request_is_exact(db,make_user):
    from app.warehouse import service
    keeper,producer,m,p,b,b2,digest,doc=setup(db,make_user)
    op=service.issue_by_techcard(db,p,keeper,received_by='Old',lines=[(m.id,2)]);db.commit()
    movement=db.query(StockMovement).filter_by(operation_id=op.id).one()
    with pytest.raises(HTTPException):c.return_material(db,movement.id,return_body(doc,1),keeper)
    req=request_material(db,b,1,producer);service.approve_request(db,req,keeper);db.commit()
    legacy=db.query(StockMovement).filter_by(reference_id=req.id).one()
    c.return_material(db,legacy.id,return_body(doc,1),keeper);db.commit()
    assert c.returned_quantity(db,legacy.id)==1


def test_foreign_document_and_bad_file_upload_are_rejected(db,make_user,api):
    keeper,_,m,p,b,b2,digest,doc=setup(db,make_user)
    client=api(keeper)
    response=client.post('/api/warehouse/control/documents',files={'file':('fake.pdf',b'%PDF-not-real','application/pdf')})
    assert response.status_code==422
    assert client.get('/api/warehouse/control/documents/999999').status_code==404


def test_changed_mapping_blocks_return_without_partial_mutation(db,make_user):
    keeper,_,m,p,b,b2,digest,doc=setup(db,make_user)
    op=c.issue(db,p.id,issue_body(digest,m,6),keeper);db.commit()
    movement=db.query(StockMovement).filter_by(operation_id=op.id).one()
    b2.quantity_provided=0;db.commit()
    with pytest.raises(HTTPException):c.return_material(db,movement.id,return_body(doc,6),keeper)
    db.rollback();db.refresh(b);db.refresh(m)
    assert b.quantity_provided==4 and m.quantity_in_stock==24


def test_returns_are_visible_with_verified_evidence(db,make_user,api):
    keeper,_,m,p,b,b2,digest,doc=setup(db,make_user)
    op=c.issue(db,p.id,issue_body(digest,m,6),keeper);db.commit()
    movement=db.query(StockMovement).filter_by(operation_id=op.id).one()
    c.return_material(db,movement.id,return_body(doc),keeper);db.commit()
    row=api(keeper).get('/api/warehouse/control/returns').json()[0]
    assert row['remaining']=='3' and row['returns'][0]['intact']
    assert row['returns'][0]['proof']['sha256']==doc.sha256


@pytest.mark.parametrize('quantity',['0.0000001','NaN','Infinity','1000000001'])
def test_issue_precision_and_nonfinite_quantities_rejected(db,make_user,api,quantity):
    keeper,_,m,p,b,b2,digest,_=setup(db,make_user)
    body=issue_body(digest,m,1).model_dump(mode='json');body['lines'][0]['quantity']=quantity
    result=api(keeper).post(f'/api/warehouse/techcard-issue/{p.id}',json=body)
    assert result.status_code==422
    assert m.quantity_in_stock==30 and not db.query(StockMovement).count()
