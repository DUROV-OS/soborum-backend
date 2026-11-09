from pathlib import Path
from uuid import uuid4
import pytest
from fastapi import HTTPException
from app.common.module_access import Module
from app.production.service import request_material
from app.warehouse import controls as c, service as s, reconciliation as r
from app.warehouse.control_models import WarehouseControlRecord as Record, WarehouseInventoryAct as Act
from app.warehouse.models import StockDocument, StockMovement, StockMovementReason
from tests.test_warehouse_control import setup, issue_body, return_body
from tests.warehouse_control_helpers import inventory_act, evidence
from tests.test_stock_documents import setup as count_setup, create as count_create
from tests.test_warehouse_issue import _block, _norm, _house


def legacy(db, make_user, quantity=6):
    keeper, producer, m, p, b, b2, digest, doc = setup(db, make_user)
    op = s.issue_by_techcard(db, p, keeper, received_by='Legacy recipient', lines=[(m.id, quantity)])
    db.commit()
    move = db.query(StockMovement).filter_by(operation_id=op.id).one()
    return keeper, producer, m, p, b, b2, doc, move


def confirm_body(db, move, doc, pairs):
    return r.Confirm(request_id=uuid4(), source_hash=r.plan(db, move)['source_hash'], file_id=doc.id,
        reason='Signed historical statement checked', confirmed=True,
        allocations=[{'block_material_id': bid, 'quantity': qty, 'locator': f'Page 1, block {bid}'} for bid, qty in pairs])


def test_count_requires_scan_and_personal_confirmation(db, make_user, api):
    client, owner, m, _ = count_setup(db, api, make_user)
    doc, _ = count_create(client, m, kind='inventory', quantity='8')
    body={'version':doc['version'],'request_id':str(uuid4())}
    assert client.post(f"/api/warehouse/documents/{doc['id']}/post",json=body).status_code==422
    assert m.quantity_in_stock==10 and not db.query(StockMovement).count()
    proof=inventory_act(client,doc)
    response=client.post(f"/api/warehouse/documents/{doc['id']}/post",json={**body,**proof})
    assert response.status_code==200,response.text
    db.refresh(m);assert m.quantity_in_stock==8
    assert client.post(f"/api/warehouse/documents/{doc['id']}/post",json={**body,**proof}).json()==response.json()
    assert client.post(f"/api/warehouse/documents/{doc['id']}/post",json={**body,**proof,'confirmed':False}).status_code==409
    history=client.get(f"/api/warehouse/inventory-evidence/{doc['id']}").json()
    assert [a['stage'] for a in history]==['posted','prepared'] and history[0]['current']
    assert history[0]['snapshot']['lines'][0]['quantity']=='8'


def test_clerk_prepares_act_full_role_posts_and_archiving_preserves_evidence(db, make_user, api):
    client, owner, m, _=count_setup(db,api,make_user)
    clerk=make_user(Module.WAREHOUSE)
    client=api(clerk);doc,_=count_create(client,m,kind='inventory',quantity='8')
    proof=inventory_act(client,doc);body={'version':doc['version'],'request_id':str(uuid4()),**proof}
    assert client.post(f"/api/warehouse/documents/{doc['id']}/post",json=body).status_code==403
    result=api(owner).post(f"/api/warehouse/documents/{doc['id']}/post",json=body)
    assert result.status_code==200,result.text
    posted=result.json()
    assert api(owner).post(f"/api/warehouse/documents/{doc['id']}/archive",json={'version':posted['version'],'archived':True}).status_code==200
    assert api(owner).get(f"/api/warehouse/inventory-evidence/{doc['id']}").json()[0]['current']


def test_count_edit_or_corrupt_scan_invalidates_act(db, make_user, api):
    client, owner, m, _=count_setup(db,api,make_user)
    doc,_=count_create(client,m,kind='inventory',quantity='8');proof=inventory_act(client,doc)
    changed=client.patch(f"/api/warehouse/documents/{doc['id']}",json={'version':doc['version'],'note':doc['note'],'lines':[{'material_id':m.id,'quantity':'7'}]}).json()
    assert client.post(f"/api/warehouse/documents/{doc['id']}/post",json={'version':changed['version'],'request_id':str(uuid4()),**proof}).status_code==409
    next_proof=inventory_act(client,changed)
    from app.common.files import FileAsset
    scan=db.get(FileAsset,db.get(Act,next_proof['act_id']).file_id)
    Path(scan.path_on_disk).write_bytes(b'changed')
    assert client.post(f"/api/warehouse/documents/{doc['id']}/post",json={'version':changed['version'],'request_id':str(uuid4()),**next_proof}).status_code==409
    assert m.quantity_in_stock==10


def test_count_snapshot_still_rejects_stock_roundtrip_and_can_fix_negative_stock(db, make_user, api):
    client, owner, m, _=count_setup(db,api,make_user)
    m.quantity_in_stock=-2;db.commit()
    doc,_=count_create(client,m,kind='inventory',quantity='3');proof=inventory_act(client,doc)
    # The quantity comes back to the baseline, but two real movements make the count stale.
    for delta in (1,-1):
        m.quantity_in_stock+=delta
        s.log_movement(db,m,delta,StockMovementReason.RECEIPT if delta>0 else StockMovementReason.WRITE_OFF,owner)
    db.commit()
    assert client.post(f"/api/warehouse/documents/{doc['id']}/post",json={'version':doc['version'],'request_id':str(uuid4()),**proof}).status_code==409
    refreshed=client.post(f"/api/warehouse/documents/{doc['id']}/recount",json={'version':doc['version']}).json()
    refreshed=client.patch(f"/api/warehouse/documents/{doc['id']}",json={'version':refreshed['version'],'note':'New physical count','lines':[{'material_id':m.id,'quantity':'3'}]}).json()
    assert client.post(f"/api/warehouse/documents/{doc['id']}/post",json={'version':refreshed['version'],'request_id':str(uuid4()),**inventory_act(client,refreshed)}).status_code==200
    db.refresh(m);assert m.quantity_in_stock==3


def test_legacy_attribution_preserves_stock_and_enables_exact_return(db, make_user, api):
    keeper,producer,m,p,b,b2,doc,move=legacy(db,make_user)
    body=confirm_body(db,move,doc,[(b.id,4),(b2.id,2)])
    before=(m.quantity_in_stock,b.quantity_provided,b2.quantity_provided,db.query(StockMovement).count())
    record=r.confirm(db,move.id,body,producer);db.commit()
    assert r.confirm(db,move.id,body,producer).id==record.id
    assert before==(m.quantity_in_stock,b.quantity_provided,b2.quantity_provided,db.query(StockMovement).count())
    assert c.original_allocations(db,move)[0]==p.id
    c.return_material(db,move.id,return_body(doc,5),keeper);db.commit()
    assert (b.quantity_required,b.quantity_provided,b2.quantity_required,b2.quantity_provided,m.quantity_in_stock)==(4,0,5,1,29)
    assert not r.plan(db,move)['can_change']
    assert api(keeper).get('/api/warehouse/control/reconciliation').json()['items'][0]['previous']['intact']


@pytest.mark.parametrize('pairs', ['too_much','too_little','duplicate','foreign','block_cap'])
def test_legacy_allocation_limits(db,make_user,pairs):
    keeper,producer,m,p,b,b2,doc,move=legacy(db,make_user)
    other=_norm(db,_block(db,_house(db),'Foreign',1),m,0,provided=20);db.commit()
    values={'too_much':[(b.id,4),(b2.id,3)],'too_little':[(b.id,4)],'duplicate':[(b.id,3),(b.id,3)],'foreign':[(other.id,6)],'block_cap':[(b.id,6)]}[pairs]
    with pytest.raises(HTTPException):r.confirm(db,move.id,confirm_body(db,move,doc,values),producer)
    db.rollback()
    assert db.query(Record).filter_by(kind='reconcile').count()==0 and m.quantity_in_stock==24


def test_named_production_only_stale_source_and_new_issue_cannot_be_relabelled(db, make_user, api):
    keeper,producer,m,p,b,b2,doc,move=legacy(db,make_user)
    body=confirm_body(db,move,doc,[(b.id,4),(b2.id,2)])
    response=api(make_user(admin=True)).post(f'/api/warehouse/control/reconciliation/{move.id}',json=body.model_dump(mode='json'))
    assert response.status_code==403
    b.quantity_provided=3;db.flush()
    with pytest.raises(HTTPException) as exc:r.confirm(db,move.id,body,producer)
    assert exc.value.status_code==409;db.rollback()
    op=c.issue(db,p.id,issue_body(c.state(db,p.id)['source_hash'],m,2),keeper);db.commit()
    new=db.query(StockMovement).filter_by(operation_id=op.id).one()
    with pytest.raises(HTTPException):r.plan(db,new)


def test_capacity_subtracts_other_issues_and_returns(db, make_user):
    keeper,producer,m,p,b,b2,doc,move=legacy(db,make_user,3)
    req=request_material(db,b,1,producer);s.approve_request(db,req,keeper);db.commit()
    plan=r.plan(db,move)
    first=next(x for x in plan['blocks'] if x['block_material_id']==b.id)
    assert first['provided']=='4' and first['attributed_elsewhere']=='1' and first['available']=='3'
    r.confirm(db,move.id,confirm_body(db,move,doc,[(b.id,3)]),producer);db.commit()
    c.return_material(db,move.id,return_body(doc,1),keeper);db.commit()
    op=s.issue_by_techcard(db,p,keeper,received_by='Later legacy',lines=[(m.id,1)]);db.commit()
    later=db.query(StockMovement).filter_by(operation_id=op.id).one()
    first=next(x for x in r.plan(db,later)['blocks'] if x['block_material_id']==b.id)
    assert first['attributed_elsewhere']=='3' and first['available']=='1'


def test_reconciliation_correction_is_append_only_and_damage_blocks_return(db,make_user):
    keeper,producer,m,p,b,b2,doc,move=legacy(db,make_user)
    first=r.confirm(db,move.id,confirm_body(db,move,doc,[(b.id,4),(b2.id,2)]),producer);db.commit()
    second=r.confirm(db,move.id,confirm_body(db,move,doc,[(b.id,4),(b2.id,2)]),producer);db.commit()
    assert second.content['replaces']==first.id and db.query(Record).filter_by(kind='reconcile').count()==2
    Path(doc.path_on_disk).write_bytes(b'bad evidence')
    with pytest.raises(HTTPException):c.return_material(db,move.id,return_body(doc,1),keeper)
    assert m.quantity_in_stock==24


def test_count_rejects_foreign_act_and_reports_movement_changes(db,make_user,api):
    client,owner,m,_=count_setup(db,api,make_user)
    first,_=count_create(client,m,kind='inventory',quantity='8')
    second,_=count_create(client,m,kind='inventory',quantity='9')
    proof=inventory_act(client,first)
    assert client.post(f"/api/warehouse/documents/{second['id']}/post",json={'version':second['version'],'request_id':str(uuid4()),**proof}).status_code==409
    assert client.post(f"/api/warehouse/documents/{first['id']}/post",json={'version':first['version'],'request_id':str(uuid4()),**proof}).status_code==200
    db.query(StockMovement).one().delta=-1;db.commit()
    history=client.get(f"/api/warehouse/inventory-evidence/{first['id']}").json()
    assert history[0]['intact'] and not history[0]['current']
