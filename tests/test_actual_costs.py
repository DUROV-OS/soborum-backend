from copy import deepcopy
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from uuid import uuid4
import pytest
from pypdf import PdfWriter
from app.accounting.actual_models import ActualCostVersion, ActualCostDecision
from app.accounting.models import MoneyMovement, MoneyDirection, MoneySubkind, MoneyMovementStatus
from app.common.files import FileAsset
from app.common.module_access import Module, AccessLevel
from app.costing.models import CostingEstimate, CostingRevision
from app.costing.governance_models import CostingApprovalPolicy
from app.production.models import Production
from app.purchasing.models import PurchaseReceipt, PurchaseReturn
from app.warehouse.models import WarehouseMaterial, Warehouse, MaterialCategory, WarehouseOperation, WarehouseOperationKind, StockMovement, StockMovementReason

P='/api/accounting/actual-costs'


@pytest.fixture
def f(db,api,make_user,default_account,tmp_path,monkeypatch):
    from app.common import files
    monkeypatch.setattr(files.settings,'storage_dir',str(tmp_path))
    owner=make_user(admin=True);accountant=make_user(Module.ACCOUNTING)
    db.add(CostingApprovalPolicy(id=1,accounting_user_id=accountant.id,director_user_id=owner.id))
    project=CostingEstimate(title='Синтетический проект',actor_id=owner.id)
    db.add(project);db.commit()
    r=api(owner).post('/api/clients/',json={'full_name':'Синтетический клиент','phone':'79990000000','email':'cost@test.invalid'})
    assert r.status_code==201,r.text
    from app.clients.models import Client
    client=db.get(Client,r.json()['id']);project.client_id=client.id
    production=Production(cycle_id=client.cycle_id,name='Синтетическое производство')
    material=WarehouseMaterial(title='Доска',code='COST-TEST',warehouse=Warehouse.TECHNOLOGY,category=MaterialCategory.NONE,unit='шт',quantity_in_stock=8,purchase_price=999,threshold=0)
    db.add_all([production,material]);db.flush()
    at=datetime(2026,11,1,10,tzinfo=timezone.utc)
    incoming=WarehouseOperation(kind=WarehouseOperationKind.RECEIPT,occurred_at=at,created_by_id=owner.id)
    outgoing=WarehouseOperation(kind=WarehouseOperationKind.ISSUE_TECHCARD,occurred_at=at,created_by_id=owner.id,production_id=production.id)
    db.add_all([incoming,outgoing]);db.flush()
    receipt=PurchaseReceipt(actor_id=owner.id,operation_id=incoming.id,occurred_at=at,status='posted',lines=[{'material_id':material.id,'stock_tracked':True,'quantity':10,'unit_price':12.505,'title':'Доска','unit':'шт'}])
    movement=StockMovement(warehouse_material_id=material.id,delta=-2,reason=StockMovementReason.ISSUED_TECHCARD,operation_id=outgoing.id,created_by_id=owner.id)
    inbound=StockMovement(warehouse_material_id=material.id,delta=10,reason=StockMovementReason.RECEIPT,operation_id=incoming.id,created_by_id=owner.id)
    db.add_all([receipt,movement,inbound]);db.commit()
    stream=BytesIO();writer=PdfWriter();writer.add_blank_page(width=100,height=100);writer.write(stream)
    r=api(accountant).post(P+'/documents',files={'file':('test.pdf',stream.getvalue(),'application/pdf')});assert r.status_code==201,r.text
    return dict(db=db,api=api,owner=owner,accountant=accountant,project=project,production=production,account=default_account,file_id=r.json()['file_id'],receipt=receipt,movement=movement,material=material,make_user=make_user,pdf=stream.getvalue())


def body(f,**changes):
    return {'request_id':str(uuid4()),'expected_version':0,'kind':'document','organization_id':f['account'].organization_id,
            'issuer':'Подрядчик ИНН 1234567890','number':'Акт-1','document_date':'2026-11-01','line_ref':'строка 1','incurred_on':'2026-11-01',
            'evidence':{'file_id':f['file_id'],'locator':'Страница 1, итог и подписи'},'gross_amount':'100.01','excluded_tax':'0','tax_basis':'Налог не исключается; строка 1 документа',
            'allocations':[{'category':'work','project_id':f['project'].id,'month':None,'amount':'100.01','locator':'Страница 1, строка 1'}],
            'reason':'Работы приняты по акту; синтетический тест',**changes}


def create(f,data=None):
    r=f['api'](f['accountant']).post(P+'/records',json=data or body(f));assert r.status_code==201,r.text;return r.json()


def decision(f,row,action='recognize',user=None):
    return f['api'](user or f['accountant']).post(P+f'/records/{row["id"]}/decisions',json={'request_id':str(uuid4()),'version':row['version'],'source_hash':row['source_hash'],'action':action,'confirmed':True,'reason':'Документы и фактическое исполнение лично сверены'})


def material_body(f,quantity='2'):
    from decimal import Decimal, ROUND_HALF_UP
    amount=str((Decimal(quantity)*Decimal('12.505')).quantize(Decimal('.01'),rounding=ROUND_HALF_UP))
    return body(f,kind='material',receipt_id=f['receipt'].id,receipt_line=0,movement_id=f['movement'].id,quantity=quantity,gross_amount=amount,
                allocations=[{'category':'materials','project_id':f['project'].id,'month':None,'amount':amount,'locator':'Накладная и ведомость выдачи, строка 1'}])


def test_payment_not_cost_and_named_personal_recognition(f):
    payment=MoneyMovement(direction=MoneyDirection.EXPENSE,subkind=MoneySubkind.SUPPLY_PAYMENT,amount=100.01,tax=0,status=MoneyMovementStatus.POSTED,account_id=f['account'].id,initiator_id=f['owner'].id)
    f['db'].add(payment);f['db'].commit()
    row=create(f)
    assert row['review']['state']=='pending'
    view=f['api'](f['owner']).get(P).json()
    assert view['projects'][0]['recognized_direct']=='0.00' and not view['cash_added_to_cost']
    assert decision(f,row,user=f['owner']).status_code==403
    assert decision(f,row).status_code==200
    view=f['api'](f['owner']).get(P).json()
    assert view['projects'][0]['recognized_direct']=='100.01'
    assert view['projects'][0]['actual_profit'] is None and view['projects'][0]['final_cost'] is None
    assert f['api'](f['owner']).get(P+'/options').status_code==200


def test_duplicate_idempotency_and_different_scan_identity(f):
    data=body(f);row=create(f,data)
    assert create(f,data)['id']==row['id']
    assert f['db'].query(ActualCostVersion).count()==1
    assert f['api'](f['accountant']).post(P+'/records',json=body(f)).status_code==409
    changed=body(f,number='Другое обозначение')
    assert f['api'](f['accountant']).post(P+'/records',json=changed).status_code==409
    upload=f['api'](f['accountant']).post(P+'/documents',files={'file':('same.pdf',f['pdf'],'application/pdf')}).json()
    changed['evidence']['file_id']=upload['file_id']
    assert f['api'](f['accountant']).post(P+'/records',json=changed).status_code==409


def test_correction_preserves_history_and_requires_new_review(f):
    row=create(f);assert decision(f,row).status_code==200
    changed=body(f,expected_version=1);changed['allocations'][0]['category']='transport'
    url=P+f'/records/{row["id"]}/versions'
    assert f['api'](f['accountant']).post(url,json=changed).status_code==409
    assert decision(f,row,'void').status_code==200
    assert f['api'](f['owner']).get(P).json()['projects'][0]['recognized_direct']=='0.00'
    r=f['api'](f['accountant']).post(url,json=changed);assert r.status_code==201,r.text
    new=r.json();assert new['review']['state']=='pending' and len(new['history'])==2
    assert decision(f,row).status_code==409
    assert decision(f,new).status_code==200
    assert f['db'].query(ActualCostVersion).count()==2 and f['db'].query(ActualCostDecision).count()==3


def test_file_tampering_removes_recognition(f):
    row=create(f);assert decision(f,row).status_code==200
    Path(f['db'].get(FileAsset,f['file_id']).path_on_disk).write_bytes(b'changed')
    view=f['api'](f['owner']).get(P).json()
    assert view['records'][0]['review']['state']=='stale'
    assert view['projects'][0]['recognized_direct']=='0.00'
    assert f['api'](f['owner']).get(P+f'/documents/{f["file_id"]}').status_code==409


@pytest.mark.parametrize('change',[{'excluded_tax':'100.01'},{'gross_amount':'100.02'},{'incurred_on':'2099-01-01'},{'receipt_id':1},{'evidence':{'file_id':999999,'locator':'Страница 1'}}])
def test_invalid_or_unsupported_values_rejected(f,change):
    assert f['api'](f['accountant']).post(P+'/records',json=body(f,**change)).status_code==422


def test_material_cost_from_receipt_not_current_price(f):
    row=create(f,material_body(f))
    assert row['net_amount']=='25.01'
    assert decision(f,row).status_code==200
    f['material'].purchase_price=999999;f['db'].commit()
    view=f['api'](f['owner']).get(P).json()
    assert view['projects'][0]['recognized_direct']=='25.01'
    assert view['records'][0]['review']['state']=='recognized'


def test_production_return_invalidates_cost_and_caps_new_recognition(f):
    from tests.test_warehouse_issue import _block, _norm
    from tests.warehouse_control_helpers import approved, evidence
    from tests.test_warehouse_control import issue_body, return_body
    from app.warehouse import controls as c
    db=f['db'];p=f['production'];m=f['material'];owner=f['owner']
    _norm(db,_block(db,p,'Return test',1),m,4)
    digest,_,scan=approved(db,p.id,owner)
    body_issue=issue_body(digest,m,2)
    body_issue.occurred_at=f['receipt'].occurred_at
    op=c.issue(db,p.id,body_issue,owner);db.commit()
    f['movement']=db.query(StockMovement).filter_by(operation_id=op.id).one()
    row=create(f,material_body(f));assert decision(f,row).status_code==200
    c.return_material(db,f['movement'].id,return_body(scan,1),owner);db.commit()
    report=f['api'](owner).get(P).json()
    assert report['records'][0]['review']['state']=='stale'
    assert report['projects'][0]['recognized_direct']=='0.00'
    assert decision(f,row,'void').status_code==200
    corrected=material_body(f,'1');corrected['expected_version']=1
    response=f['api'](f['accountant']).post(P+f'/records/{row["id"]}/versions',json=corrected)
    assert response.status_code==201,response.text
    assert decision(f,response.json()).status_code==200
    options=f['api'](owner).get(P+'/options').json()
    assert next(v for v in options['movements'] if v['id']==f['movement'].id)['quantity']=='1'


def test_material_quantity_caps_and_wrong_price(f):
    assert f['api'](f['accountant']).post(P+'/records',json=material_body(f,'3')).status_code==409
    data=material_body(f);data['gross_amount']='100';data['allocations'][0]['amount']='100'
    assert f['api'](f['accountant']).post(P+'/records',json=data).status_code==422
    row=create(f,material_body(f));assert decision(f,row).status_code==200
    # A second party cannot account for an already fully recognized issue.
    receipt=PurchaseReceipt(actor_id=f['owner'].id,operation_id=f['receipt'].operation_id,occurred_at=f['receipt'].occurred_at,status='posted',lines=deepcopy(f['receipt'].lines))
    f['db'].add(receipt);f['db'].commit()
    data=material_body(f,'1');data['receipt_id']=receipt.id
    assert f['api'](f['accountant']).post(P+'/records',json=data).status_code==409


def test_receipt_mutation_invalidates_material_review(f):
    row=create(f,material_body(f));assert decision(f,row).status_code==200
    lines=deepcopy(f['receipt'].lines);lines[0]['unit_price']=20;f['receipt'].lines=lines;f['db'].commit()
    view=f['api'](f['owner']).get(P).json()
    assert view['records'][0]['review']['state']=='stale' and view['projects'][0]['recognized_direct']=='0.00'


def test_material_requires_actual_project_issue(f):
    other=CostingEstimate(title='Другой проект без клиента',actor_id=f['owner'].id);f['db'].add(other);f['db'].commit()
    data=material_body(f);data['allocations'][0]['project_id']=other.id
    assert f['api'](f['accountant']).post(P+'/records',json=data).status_code==422
    f['movement'].reason=StockMovementReason.WRITE_OFF;f['db'].commit()
    assert f['api'](f['accountant']).post(P+'/records',json=material_body(f)).status_code==422


def test_direct_cost_split_and_fixed_month_scope(f):
    other=CostingEstimate(title='Второй проект',actor_id=f['owner'].id);f['db'].add(other);f['db'].commit()
    data=body(f);data['allocations']=[{**data['allocations'][0],'amount':'50.00'},{**data['allocations'][0],'project_id':other.id,'amount':'50.01'}]
    row=create(f,data);assert decision(f,row).status_code==200
    amounts={p['id']:p['recognized_direct'] for p in f['api'](f['owner']).get(P).json()['projects']}
    assert amounts=={f['project'].id:'50.00',other.id:'50.01'}
    data=body(f,line_ref='строка 2');data['allocations'][0]['category']='factory'
    assert f['api'](f['accountant']).post(P+'/records',json=data).status_code==422


def test_financial_permissions_and_read_only_access(f):
    worker=f['make_user'](Module.WAREHOUSE,Module.PRODUCTION)
    for path in ('','/options','/records/1',f'/documents/{f["file_id"]}'):
        assert f['api'](worker).get(P+path).status_code==403
    reader=f['make_user'](Module.ACCOUNTING,level=AccessLevel.VIEW)
    assert f['api'](reader).get(P).status_code==200
    assert f['api'](reader).post(P+'/records',json=body(f)).status_code==403


def closing_project(f):
    """Exercise real monthly approvals, documented costs and a real shipment fixture."""
    from app.accounting.models import Counterparty, CounterpartyKind
    from app.sales.models import SalesOrder, SalesShipment
    from app.accounting.actual_schemas import CATEGORIES
    G='/api/costing/governance';owner=f['api'](f['owner'])
    r=owner.post(G+'/month-plans',json={'month':'2026-11-01','planned_units':10,'factory':'100','warehouse':'0','staff':'100','other':'0','note':'Синтетический план'});assert r.status_code==201,r.text
    plan=r.json()
    r=owner.post(G+f'/documents/month/{plan["id"]}',files={'file':('test.pdf',f['pdf'],'application/pdf')});assert r.status_code==201,r.text
    doc=r.json()
    r=f['api'](f['accountant']).post(G+f'/month-plans/{plan["id"]}/accounting',json={'request_id':str(uuid4()),'version':1,'reason':'Документы плана сверены','evidence':[{'key':x['key'],'value':x['value'],'document_id':doc['id'],'locator':'Страница 1, '+x['key']} for x in plan['facts']]});assert r.status_code==200,r.text
    r=f['api'](f['owner']).post(G+f'/month-plans/{plan["id"]}/director',json={'request_id':str(uuid4()),'version':1,'reason':'План утверждён в тесте'});assert r.status_code==200,r.text
    for category in ('factory','staff'):
        data=body(f,line_ref=category,gross_amount='100',allocations=[{'category':category,'amount':'100','project_id':None,'month':'2026-11-01','locator':'Акт за ноябрь, '+category}])
        row=create(f,data);assert decision(f,row).status_code==200
    row=create(f);assert decision(f,row).status_code==200
    material=create(f,material_body(f));assert decision(f,material).status_code==200
    db=f['db'];project=f['project'];project.status='approved'
    cp=Counterparty(name='Тестовый покупатель',kind=CounterpartyKind.CLIENT);db.add(cp);db.flush()
    order=SalesOrder(actor_id=f['owner'].id,counterparty_id=cp.id,organization_id=f['account'].organization_id,status='confirmed',total=2000,
        occurred_at=datetime(2026,11,1,tzinfo=timezone.utc),lines=[{'quantity':'2','unit_price':'1000','line_total':'2000'}])
    db.add(order);db.flush();project.sales_order_id=order.id;project.sales_order_line=0
    rev=CostingRevision(estimate_id=project.id,number=1,actor_id=f['owner'].id,reason='Синтетический ранее согласованный расчёт',request_id=str(uuid4()),request_data={'overhead_plan_id':plan['id'],'quantity':'2'},snapshot={'result':{'cost':'200','price':'2000'}})
    shipment=SalesShipment(actor_id=f['owner'].id,order_id=order.id,counterparty_id=cp.id,organization_id=f['account'].organization_id,status='posted',total=2000,
        occurred_at=datetime(2026,11,5,tzinfo=timezone.utc),lines=[{'order_line':0,'quantity':'2','unit_price':'1000','line_total':'2000'}])
    db.add_all([rev,shipment]);db.commit()
    # The order was bound after cost recognition: refresh versions, as production does.
    for old in (row,material):
        assert decision(f,old,'void').status_code==200
        data={**old['data'],'request_id':str(uuid4()),'expected_version':1}
        r=f['api'](f['accountant']).post(P+f'/records/{old["id"]}/versions',json=data);assert r.status_code==201,r.text
        assert decision(f,r.json()).status_code==200
    report=f['api'](f['owner']).get(P).json()['projects'][0]
    assert report['recognized_direct']=='125.02' and report['allocated_overhead']=='40.00' and not report['issues']
    present={**report['categories'],**report['overhead']['categories']}
    proof={'file_id':f['file_id'],'locator':'Страница 1, сверка результата'}
    request={'request_id':str(uuid4()),'source_hash':report['source_hash'],'confirmed':True,'reason':'Полный состав затрат и выручка лично сверены',
             'completion_evidence':proof,'zero_categories':[{'category':key,'evidence':proof} for key in CATEGORIES if float(present.get(key,0))==0],
             'production_ids':[f['production'].id],'shipment_ids':[shipment.id],'revenue_evidence':proof,'revenue_excluded_tax':'200','revenue_tax_basis':'Расчёт налога в документе, строка 1'}
    return report,request,shipment


def test_full_actual_cost_close_requires_accountant_then_director(f):
    report,request,shipment=closing_project(f);id=f['project'].id
    assert f['api'](f['owner']).post(P+f'/projects/{id}/accounting-close',json=request).status_code==403
    r=f['api'](f['accountant']).post(P+f'/projects/{id}/accounting-close',json=request);assert r.status_code==200,r.text
    checked=r.json();assert checked['closure']['state']=='awaiting_director' and checked['actual_profit'] is None
    director={'request_id':str(uuid4()),'source_hash':checked['source_hash'],'accounting_closure_id':checked['closure']['accounting_id'],'confirmed':True,'reason':'Проверил итог проекта'}
    assert f['api'](f['accountant']).post(P+f'/projects/{id}/director-close',json=director).status_code==403
    r=f['api'](f['owner']).post(P+f'/projects/{id}/director-close',json=director);assert r.status_code==200,r.text
    result=r.json();assert result['complete'] and result['final_cost']=='165.02' and result['actual_profit']=='1634.98' and result['net_revenue']=='1800.00'
    assert f['api'](f['owner']).post(P+f'/projects/{id}/director-close',json=director).status_code==200
    shipment.status='cancelled';f['db'].commit()
    result=f['api'](f['owner']).get(P).json()['projects'][0]
    assert not result['complete'] and result['actual_profit'] is None and result['closure']['state']=='stale'


@pytest.mark.parametrize('missing',['zeros','production','partial_shipment','unrecognized_cost'])
def test_incomplete_project_cannot_be_closed(f,missing):
    report,request,shipment=closing_project(f)
    if missing=='zeros': request['zero_categories']=[]
    if missing=='production': request['production_ids']=[]
    if missing=='partial_shipment':
        shipment.lines=[{'order_line':0,'quantity':'1','unit_price':'1000','line_total':'1000'}];f['db'].commit()
    if missing=='unrecognized_cost':
        create(f,body(f,line_ref='новый расход'))
        request['source_hash']=f['api'](f['owner']).get(P).json()['projects'][0]['source_hash']
    r=f['api'](f['accountant']).post(P+f'/projects/{f["project"].id}/accounting-close',json=request)
    assert r.status_code in (409,422),r.text


def test_new_month_expense_invalidates_closed_result_and_remainder_is_explicit(f):
    report,request,_=closing_project(f);id=f['project'].id
    r=f['api'](f['accountant']).post(P+f'/projects/{id}/accounting-close',json=request);assert r.status_code==200,r.text
    checked=r.json()
    director={'request_id':str(uuid4()),'source_hash':checked['source_hash'],'accounting_closure_id':checked['closure']['accounting_id'],'confirmed':True,'reason':'Итог сверил'}
    assert f['api'](f['owner']).post(P+f'/projects/{id}/director-close',json=director).json()['complete']
    row=create(f,body(f,line_ref='дополнительный цех',gross_amount='50',allocations=[{'category':'factory','amount':'50','month':'2026-11-01','project_id':None,'locator':'Новая строка акта'}]));assert decision(f,row).status_code==200
    result=f['api'](f['owner']).get(P).json()['projects'][0]
    assert result['allocated_overhead']=='50.00' and result['closure']['state']=='stale' and not result['complete']


def test_over_allocation_month_plan_blocks_close(f):
    report,request,_=closing_project(f)
    revision=f['db'].query(CostingRevision).filter_by(estimate_id=f['project'].id).one()
    revision.request_data={**revision.request_data,'quantity':'11'};f['db'].commit()
    result=f['api'](f['owner']).get(P).json()['projects'][0]
    assert result['allocated_overhead'] is None and any('превышает' in i for i in result['issues'])


def test_month_kopecks_reconcile_exactly_and_keep_unallocated_share():
    from decimal import Decimal as D
    from app.accounting.actual import allocate_month
    parts=allocate_month({'factory':D('100.01'),'staff':D('200.02')},{2:D(1),1:D(1)},3)
    assert sum((sum(p.values(),D(0)) for p in parts.values()),D(0))==D('300.03')
    assert parts[None]['factory']==D('33.33')
    assert parts[1]['factory']==parts[2]['factory']==D('33.34')


def test_partial_material_issues_reconcile_kopecks_and_reversal_invalidates_dependents(f):
    f['movement'].delta=-1;f['db'].commit()
    first=create(f,material_body(f,'1'));assert first['net_amount']=='12.51'
    assert decision(f,first).status_code==200
    second=StockMovement(warehouse_material_id=f['material'].id,delta=-1,reason=StockMovementReason.ISSUED_TECHCARD,operation_id=f['movement'].operation_id,created_by_id=f['owner'].id)
    f['db'].add(second);f['db'].commit()
    preview=f['api'](f['accountant']).get(P+'/material-valuation',params={'receipt_id':f['receipt'].id,'receipt_line':0,'quantity':'1'})
    assert preview.status_code==200 and preview.json()['amount']=='12.50'
    data=material_body(f,'1');data.update(movement_id=second.id,gross_amount='12.50');data['allocations'][0]['amount']='12.50'
    row=create(f,data);assert decision(f,row).status_code==200
    assert f['api'](f['owner']).get(P).json()['projects'][0]['recognized_direct']=='25.01'
    assert decision(f,first,'void').status_code==200
    result=f['api'](f['owner']).get(P).json()
    assert result['records'][0]['review']['state']=='stale'
