from copy import deepcopy
from io import BytesIO
from pathlib import Path
from uuid import uuid4
import pytest
from pypdf import PdfWriter
from app.common.files import FileAsset
from app.common.module_access import Module
from app.costing.governance_models import CostingApprovalPolicy, CostingSignoff, CostingEvidenceDocument
from app.costing.models import CostingRevision, CostingItem
from app.warehouse.models import WarehouseMaterial, Warehouse, MaterialCategory

P='/api/costing'
G=P+'/governance'


def review(version,**extra):
    return {'version':version,'request_id':str(uuid4()),'reason':'Проверено на изолированных тестовых данных',**extra}


def pdf():
    writer=PdfWriter();writer.add_blank_page(width=300,height=400)
    stream=BytesIO();writer.write(stream);return stream.getvalue()


@pytest.fixture
def flow(db,api,make_user,tmp_path,monkeypatch):
    from app.common import files
    monkeypatch.setattr(files.settings,'storage_dir',str(tmp_path))
    owner=make_user(admin=True);owner.job_role='director'
    production=make_user(Module.PRODUCTION,Module.CLIENTS);production.job_role='production_manager'
    accountant=make_user(admin=True)
    db.add(CostingApprovalPolicy(id=1,director_user_id=owner.id,production_user_id=production.id,accounting_user_id=accountant.id));db.commit()
    client=api(owner)
    material=client.post(P+'/materials',json={'name':'Доска','unit':'шт','price':'50','price_source':'Счёт поставщика','effective_on':'2026-01-01'}).json()
    expenses={key:'0' for key in ('labor','transport','subcontract','overhead','commercial','advertising','taxes','reserve')}|{'evidence':'Исходные документы проверяет бухгалтер'}
    product=WarehouseMaterial(title='Дом',code='TEST-HOUSE',warehouse=Warehouse.TECHNOLOGY,category=MaterialCategory.NONE,unit='шт',is_fractional=False,quantity_in_stock=1,purchase_price=0,threshold=0)
    db.add(product);db.commit()
    response=client.post(P+'/items',json={'kind':'template','title':'Тестовый дом','content':{'lines':[{'material_id':material['id'],'quantity':'1'}],
        'expenses':expenses,'retail_price':'1000','warehouse_product_id':product.id}})
    assert response.status_code==201,response.text
    template=response.json()
    return {'db':db,'api':api,'owner':owner,'production':production,'accountant':accountant,'material':material,'template':template,'expenses':expenses,'product':product}


def upload(f,target,id):
    response=f['api'](f['accountant']).post(f'{G}/documents/{target}/{id}',files={'file':('synthetic-evidence.pdf',pdf(),'application/pdf')})
    assert response.status_code==201,response.text
    return response.json()


def approve_month(f):
    client=f['api'](f['owner'])
    response=client.post(G+'/month-plans',json={'month':'2026-11-01','planned_units':10,'factory':'100','warehouse':'0','staff':'100','other':'0','note':'Синтетический месячный план'})
    assert response.status_code==201,response.text
    plan=response.json();doc=upload(f,'month',plan['id'])
    body=review(1,evidence=[{'key':fact['key'],'value':fact['value'],'document_id':doc['id'],'locator':'Страница 1, строка '+fact['key']} for fact in plan['facts']])
    response=f['api'](f['accountant']).post(f'{G}/month-plans/{plan["id"]}/accounting',json=body)
    assert response.status_code==200,response.text
    response=f['api'](f['owner']).post(f'{G}/month-plans/{plan["id"]}/director',json=review(1))
    assert response.status_code==200 and response.json()['approved'],response.text
    assert response.json()['per_unit']=='20.00000000'
    return plan


def draft(f,plan=None,**extra):
    body={'title':'Проект клиента','request_id':str(uuid4()),'template_id':f['template']['id'],'profile':{'name':'Условия проекта','margin':'35','labor':'10','management':'3','tax':'6','realtor':'2'},**extra}
    if plan: body['overhead_plan_id']=plan['id']
    response=f['api'](f['owner']).post(P+'/estimates',json=body)
    assert response.status_code==201,response.text
    return response.json()


def production(f,estimate):
    body=review(estimate['version'],work_scope='Сборка, проверка комплектации и технологических операций')
    response=f['api'](f['production']).post(f'{G}/estimates/{estimate["id"]}/production',json=body)
    assert response.status_code==200,response.text
    return f['api'](f['owner']).get(f'{G}/estimates/{estimate["id"]}').json(),body


def accounting(f,detail):
    doc=upload(f,'estimate',detail['id'])
    body=review(detail['version'],evidence=[{'key':fact['key'],'value':fact['value'],'document_id':doc['id'],'locator':'Страница 1, '+fact['label']} for fact in detail['facts']])
    response=f['api'](f['accountant']).post(f'{G}/estimates/{detail["id"]}/accounting',json=body)
    assert response.status_code==200,response.text
    return response.json(),doc,body


def test_no_price_computation_before_named_production_approval(flow,monkeypatch):
    f=flow
    from app.costing import service
    def forbidden(*args,**kwargs): raise AssertionError('Prices must not be calculated before Ilya')
    monkeypatch.setattr(service,'calculate_snapshot',forbidden)
    estimate=draft(f)
    assert estimate['price'] is None and estimate['result']['status']=='awaiting_production'
    assert f['api'](f['owner']).post(f'{G}/estimates/{estimate["id"]}/production',json=review(1,work_scope='Проверено')).status_code==403
    assert f['api'](f['accountant']).post(f'{G}/estimates/{estimate["id"]}/accounting',json=review(1)).status_code==409
    assert f['api'](f['owner']).post(f'{P}/estimates/{estimate["id"]}/approve',json=review(1)).status_code==409
    assert f['api'](f['owner']).get(P+'/catalog').json()[0]['price'] is None
    assert f['api'](f['owner']).post(P+'/prices',json={'items':[{'id':f['template']['id'],'version':1,'price':'1500'}],'reason':'Цена'}).status_code==409


def test_full_chain_costs_realtor_commission_and_documented_plan(flow):
    f=flow;plan=approve_month(f);estimate=draft(f,plan)
    original=deepcopy(f['db'].query(CostingRevision).filter_by(estimate_id=estimate['id'],number=1).one().snapshot)
    detail,body=production(f,estimate)
    assert detail['version']==2 and detail['result']['cost']=='280.00'
    assert detail['result']['margin_percent']=='72.00'
    assert detail['overhead_plan']['per_unit']=='20.00000000'
    assert any(x['key'].endswith(':realtor') and x['value']=='2' for x in detail['facts'])
    assert f['api'](f['production']).post(f'{G}/estimates/{estimate["id"]}/production',json=body).json()['version']==2
    restricted=f['api'](f['production']).get(f'{G}/estimates/{estimate["id"]}').json()
    assert not {'result','facts','documents','overhead_plan'}&restricted.keys()
    assert f['api'](f['production']).get(G+'/month-plans').status_code==403
    detail,doc,_=accounting(f,detail)
    assert detail['state']['accounting_approved']
    assert f['api'](f['accountant']).post(f'{P}/estimates/{detail["id"]}/approve',json=review(2)).status_code==403
    response=f['api'](f['owner']).post(f'{P}/estimates/{detail["id"]}/approve',json=review(2))
    assert response.status_code==200 and response.json()['governance']['director_approved'],response.text
    assert f['db'].query(CostingRevision).filter_by(estimate_id=estimate['id'],number=1).one().snapshot==original
    assert f['api'](f['owner']).get(f'{P}/estimates/{detail["id"]}/export/html').status_code==200


def test_missing_zero_cost_proof_wrong_number_and_tampered_scan_block_approval(flow):
    f=flow;detail,_=production(f,draft(f,approve_month(f)))
    doc=upload(f,'estimate',detail['id'])
    evidence=[{'key':x['key'],'value':x['value'],'document_id':doc['id'],'locator':'Страница 1'} for x in detail['facts']]
    client=f['api'](f['accountant'])
    assert client.post(f'{G}/estimates/{detail["id"]}/accounting',json=review(2,evidence=evidence[:-1])).status_code==422
    wrong=deepcopy(evidence);wrong[0]['value']='49'
    assert client.post(f'{G}/estimates/{detail["id"]}/accounting',json=review(2,evidence=wrong)).status_code==409
    assert client.post(f'{G}/estimates/{detail["id"]}/accounting',json=review(2,evidence=evidence)).status_code==200
    asset=f['db'].get(FileAsset,doc['file_id']);Path(asset.path_on_disk).write_bytes(b'changed scan')
    assert f['api'](f['owner']).post(f'{P}/estimates/{detail["id"]}/approve',json=review(2)).status_code==409
    assert f['api'](f['owner']).get(f'{G}/documents/{doc["id"]}/download').status_code==409


def test_technical_change_and_copy_require_new_approval(flow):
    f=flow;estimate=draft(f,approve_month(f));detail,_=production(f,estimate)
    client=f['api'](f['owner'])
    current=client.get(f'{P}/estimates/{estimate["id"]}').json()
    payload={**current['input'],'request_id':str(uuid4()),'version':2,'reason':'Увеличена норма','lines':[{'material_id':f['material']['id'],'quantity':'2'}]}
    changed=client.put(f'{P}/estimates/{estimate["id"]}',json=payload)
    assert changed.status_code==200,changed.text
    assert changed.json()['price'] is None and not changed.json()['governance']['production_approved']
    copied=client.post(f'{P}/estimates/{estimate["id"]}/copy',json=review(3))
    assert copied.status_code==201,copied.text
    assert copied.json()['price'] is None and not copied.json()['governance']['production_approved']


def test_missing_plan_and_margin_below_35_cannot_be_director_approved(flow):
    f=flow;detail,_=production(f,draft(f))
    assert detail['result']['cost'] is None
    assert any('месячный план' in x for x in detail['state']['issues'])
    detail,_=production(f,draft(f,approve_month(f),sale_price='150'))
    detail,_,_=accounting(f,detail)
    response=f['api'](f['owner']).post(f'{P}/estimates/{detail["id"]}/approve',json=review(2))
    assert response.status_code==422,response.text


def test_monthly_plan_requires_factory_staff_and_independent_signatures(flow):
    f=flow;client=f['api'](f['owner'])
    base={'month':'2026-11-01','planned_units':10,'factory':'100','warehouse':'0','staff':'100','note':'План'}
    for patch in ({'factory':'0'},{'staff':'0'},{'planned_units':0},{'month':'2026-11-02'}):
        assert client.post(G+'/month-plans',json={**base,**patch}).status_code==422
    plan=client.post(G+'/month-plans',json=base).json()
    assert client.post(f'{G}/month-plans/{plan["id"]}/director',json=review(1)).status_code==409
    assert client.post(f'{G}/month-plans/{plan["id"]}/accounting',json=review(1)).status_code==403


def test_missing_named_approvers_is_fail_closed(flow):
    f=flow;estimate=draft(f)
    policy=f['db'].get(CostingApprovalPolicy,1);policy.production_user_id=None;f['db'].commit()
    response=f['api'](f['owner']).post(f'{G}/estimates/{estimate["id"]}/production',json=review(1,work_scope='Проверено'))
    assert response.status_code==409


def test_item_approval_does_not_approve_project_and_preview_cannot_leak_unsigned_price(flow,make_user):
    f=flow
    response=f['api'](f['production']).post(f'{G}/items/{f["template"]["id"]}/production',json=review(1,work_scope='Сборка каркаса по нормам'))
    assert response.status_code==200,response.text
    assert f['api'](f['owner']).get(P+'/catalog').json()[0]['price']=='1000.00'
    manager=make_user(Module.CLIENTS);manager.job_role='manager';f['db'].commit()
    client=f['api'](manager)
    estimate=client.post(P+'/estimates',json={'title':'Смета менеджера','template_id':f['template']['id'],'request_id':str(uuid4())}).json()
    assert estimate['price'] is None
    detail,_=production(f,estimate)
    assert detail['result']['price']=='1000.00'
    client=f['api'](manager)
    read=client.get(f'{P}/estimates/{estimate["id"]}').json()
    assert read['price'] is None
    preview=client.post(f'{P}/estimates/preview?estimate_id={estimate["id"]}',json={**read['input'],'version':2,'request_id':str(uuid4())}).json()
    assert preview['price'] is None and not preview['ready']
    assert all(line['amount'] is None for line in preview['commercial'])
    assert all(line['price'] is None for line in client.get(f'{P}/estimates/{estimate["id"]}/history').json())
    assert client.get(f'{P}/estimates/{estimate["id"]}/export/html').status_code==409
    assert client.get(f'{G}/estimates/{estimate["id"]}').status_code==403


def test_source_and_month_changes_invalidate_signed_price(flow):
    f=flow;plan=approve_month(f);detail,_=production(f,draft(f,plan));detail,_,_=accounting(f,detail)
    client=f['api'](f['owner'])
    assert client.post(f'{P}/estimates/{detail["id"]}/approve',json=review(2)).status_code==200
    m=f['material']
    changed=client.put(f'{P}/materials/{m["id"]}',json={**{k:v for k,v in m.items() if k != 'id'},'price':'55'})
    assert changed.status_code==200,changed.text
    current=client.get(f'{G}/estimates/{detail["id"]}').json()
    assert not current['state']['director_approved']
    assert any('цены материала' in x for x in current['state']['issues'])
    assert client.get(f'{P}/estimates/{detail["id"]}/export/html').status_code==409
    new=client.post(G+'/month-plans',json={'month':'2026-11-01','previous_version':1,'planned_units':10,'factory':'110','warehouse':'0','staff':'100','other':'0','note':'Уточнённый план'})
    assert new.status_code==201,new.text
    assert not client.get(f'{G}/month-plans/{plan["id"]}').json()['approved']


def test_month_director_sees_document_proof_and_responsible_actor(flow):
    f=flow;plan=approve_month(f)
    history=f['api'](f['owner']).get(f'{G}/month-plans/{plan["id"]}').json()['history']
    assert [r['stage'] for r in history]==['accounting','director']
    assert len(history[0]['evidence'])==5
    assert all(r['sha256'] and r['locator'] for r in history[0]['evidence'])


def test_sale_needs_all_signatures_and_unchanged_proofs_at_confirmation(flow):
    from app.accounting.models import Counterparty,Organization
    from app.sales.models import SalesOrder
    f=flow;db=f['db'];client=f['api'](f['owner'])
    buyer=Counterparty(name='Синтетический покупатель',is_active=True);db.add(buyer);db.commit()
    org=db.query(Organization).first()
    raw=client.post('/api/sales/orders',json={'request_id':str(uuid4()),'counterparty_id':buyer.id,'organization_id':org.id,
        'lines':[{'material_id':f['product'].id,'quantity':'1','unit_price':'1000'}]})
    assert raw.status_code==201,raw.text
    assert client.post(f'/api/sales/orders/{raw.json()["id"]}/confirm',json={'version':1}).status_code==409
    detail,_=production(f,draft(f,approve_month(f)))
    detail,doc,_=accounting(f,detail);client=f['api'](f['owner'])
    assert client.post(f'{P}/estimates/{detail["id"]}/approve',json=review(2)).status_code==200
    linked=client.post(f'{P}/integration/estimates/{detail["id"]}/create-order',json={**review(2),'counterparty_id':buyer.id,'organization_id':org.id})
    assert linked.status_code==201,linked.text
    oid=linked.json()['order_id']
    asset=db.get(FileAsset,doc['file_id']);path=Path(asset.path_on_disk);original=path.read_bytes();path.write_bytes(b'replaced')
    assert client.post(f'/api/sales/orders/{oid}/confirm',json={'version':1}).status_code==409
    assert db.get(SalesOrder,oid).status=='draft'
    path.write_bytes(original)
    response=client.post(f'/api/sales/orders/{oid}/confirm',json={'version':1})
    assert response.status_code==200,response.text
    assert response.json()['status']=='confirmed'


def test_documented_service_can_be_sold_without_fake_stock_or_materials(flow):
    from app.accounting.models import Counterparty,Organization
    from app.warehouse.models import StockMovement
    from app.sales.models import SalesOrder
    f=flow;client=f['api'](f['owner'])
    response=client.post(P+'/items',json={'kind':'template','title':'Проектирование','content':{'service_only':True,'unit':'усл','expenses':f['expenses'],'retail_price':'1000'}})
    assert response.status_code==201,response.text
    f['template']=response.json()
    detail,_=production(f,draft(f,approve_month(f)))
    assert detail['bom']==[] and detail['result']['cost']=='230.00'
    detail,_,_=accounting(f,detail);client=f['api'](f['owner'])
    assert client.post(f'{P}/estimates/{detail["id"]}/approve',json=review(2)).status_code==200
    buyer=Counterparty(name='Покупатель услуги',is_active=True);f['db'].add(buyer);f['db'].commit()
    response=client.post(f'{P}/integration/estimates/{detail["id"]}/create-order',json={**review(2),'counterparty_id':buyer.id,'organization_id':f['db'].query(Organization).first().id})
    assert response.status_code==201,response.text
    oid=response.json()['order_id'];order=f['db'].get(SalesOrder,oid)
    assert order.lines[0]['stock_tracked']==False and order.lines[0]['material_id'] is None
    assert client.post(f'/api/sales/orders/{oid}/confirm',json={'version':1}).status_code==200
    assert client.post(f'{P}/integration/estimates/{detail["id"]}/production-recipe',json={**review(2),'order_id':oid,'line_index':0}).status_code==422
    assert f['db'].query(StockMovement).count()==0


def test_service_flag_cannot_hide_materials_or_inventory_product(flow):
    f=flow;client=f['api'](f['owner'])
    for patch in ({'lines':[{'material_id':f['material']['id'],'quantity':'1'}]},{'warehouse_product_id':f['product'].id}):
        response=client.post(P+'/items',json={'kind':'template','title':'Недопустимая услуга','content':{'service_only':True,**patch}})
        assert response.status_code==422,response.text
