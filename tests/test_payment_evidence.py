from copy import deepcopy
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from uuid import uuid4
import pytest
from pypdf import PdfWriter
from app.accounting.models import MoneyMovement, MoneyDirection, MoneyMovementStatus, MoneySubkind, BankAccount
from app.accounting.evidence_models import PaymentEvidenceVersion, PaymentEvidenceCheck
from app.common.files import FileAsset, FilePurpose, save_bytes_file
from app.common.module_access import Module, AccessLevel
from app.costing.governance_models import CostingApprovalPolicy

P='/api/accounting/evidence'


@pytest.fixture
def flow(db,api,make_user,default_account,tmp_path,monkeypatch):
    from app.common import files
    monkeypatch.setattr(files.settings,'storage_dir',str(tmp_path))
    owner=make_user(admin=True);accountant=make_user(Module.ACCOUNTING)
    db.add(CostingApprovalPolicy(id=1,accounting_user_id=accountant.id,director_user_id=owner.id))
    row=MoneyMovement(direction=MoneyDirection.EXPENSE,subkind=MoneySubkind.OTHER_EXPENSE,amount=100.01,tax=10,
        initiator_id=owner.id,account_id=default_account.id,status=MoneyMovementStatus.POSTED,
        doc_date=datetime(2026,11,8,tzinfo=timezone.utc),payment_purpose='Synthetic documented payment')
    db.add(row);db.commit()
    return {'db':db,'api':api,'owner':owner,'accountant':accountant,'row':row,'make_user':make_user}


def document(f,row=None):
    stream=BytesIO();pdf=PdfWriter();pdf.add_blank_page(width=100,height=100);pdf.write(stream)
    r=f['api'](f['accountant']).post(f'{P}/movements/{(row or f["row"]).id}/documents',files={'file':('proof.pdf',stream.getvalue(),'application/pdf')})
    assert r.status_code==201,r.text
    return r.json()['file_id']


def body(f,file_id):
    current=f['api'](f['accountant']).get(f'{P}/movements/{f["row"].id}').json()
    citation={'file_id':file_id,'locator':'Страница 1, сумма и налог'}
    return {'request_id':str(uuid4()),'expected_version':current['review']['version'],'source_hash':current['source_hash'],
        'nature':'expense','payment_evidence':citation,'reason':'Документы сверены с источником',
        'allocations':[{'amount':'100.01','category':'factory','scope':'month','month':'2026-11-01','evidence':citation}]}


def save(f,payload=None):
    payload=payload or body(f,document(f))
    r=f['api'](f['accountant']).post(f'{P}/movements/{f["row"].id}/versions',json=payload)
    assert r.status_code==201,r.text
    return r.json(),payload


def sign(f,version=1,user=None):
    return f['api'](user or f['accountant']).post(f'{P}/movements/{f["row"].id}/check',json={
        'request_id':str(uuid4()),'version':version,'confirmed':True,'reason':'Лично проверила сумму, налог и документы'})


def test_historical_payment_stays_unverified_until_personal_review(flow):
    f=flow;db=f['db'];row=f['row'];before=(row.amount,row.tax,row.status,row.account_id,row.updated_at)
    initial=f['api'](f['owner']).get(P+'/overview').json()
    assert initial['counts']['no_document']==1 and initial['totals'][0]['unverified_expense']=='100.01'
    assert initial['actual_profit_available'] is False and initial['bank_reconciled'] is False
    detail,_=save(f)
    assert detail['review']['state']=='pending'
    assert sign(f,user=f['owner']).status_code==403
    response=sign(f);assert response.status_code==200,response.text
    assert response.json()['review']['state']=='verified'
    db.refresh(row);assert (row.amount,row.tax,row.status,row.account_id,row.updated_at)==before
    summary=f['api'](f['owner']).get(P+'/overview?filter=verified').json()
    assert summary['total']==1 and summary['totals'][0]['unverified_expense']=='0'
    assert summary['totals'][0]['expense']=='100.01'


def test_exact_allocation_and_cross_payment_document_scope(flow):
    f=flow;file_id=document(f);payload=body(f,file_id)
    payload['allocations'][0]['amount']='100.00'
    assert f['api'](f['accountant']).post(f'{P}/movements/{f["row"].id}/versions',json=payload).status_code==422
    payload['allocations']=[{**payload['allocations'][0],'amount':'33.33'}, {**payload['allocations'][0],'amount':'66.68'}]
    detail,_=save(f,payload);assert detail['review']['state']=='pending'
    foreign=MoneyMovement(direction=MoneyDirection.EXPENSE,subkind=MoneySubkind.OTHER_EXPENSE,amount=100.01,initiator_id=f['owner'].id,status=MoneyMovementStatus.DRAFT)
    f['db'].add(foreign);f['db'].commit()
    foreign_file=document(f,foreign);payload=body(f,foreign_file)
    assert f['api'](f['accountant']).post(f'{P}/movements/{f["row"].id}/versions',json=payload).status_code==422
    assert f['api'](f['accountant']).get(f'{P}/movements/{f["row"].id}/documents/{foreign_file}').status_code==404


def test_idempotency_and_stale_version_preserve_history(flow):
    f=flow;_,payload=save(f)
    r=f['api'](f['accountant']).post(f'{P}/movements/{f["row"].id}/versions',json=payload)
    assert r.status_code==201 and f['db'].query(PaymentEvidenceVersion).count()==1
    changed=deepcopy(payload);changed['reason']='Другая версия с тем же запросом'
    assert f['api'](f['accountant']).post(f'{P}/movements/{f["row"].id}/versions',json=changed).status_code==409
    assert sign(f).status_code==200
    check=f['db'].query(PaymentEvidenceCheck).one()
    r=f['api'](f['accountant']).post(f'{P}/movements/{f["row"].id}/check',json=check.content['request'])
    assert r.status_code==200 and f['db'].query(PaymentEvidenceCheck).count()==1
    next_body=body(f,payload['payment_evidence']['file_id']);next_body['reason']='Уточнено отнесение по документам'
    new,_=save(f,next_body)
    assert new['review']['state']=='pending' and new['review']['version']==2
    assert len(new['versions'])==2 and new['versions'][1]['check']
    assert sign(f,version=1).status_code==409
    changed['request_id']=str(uuid4())
    assert f['api'](f['accountant']).post(f'{P}/movements/{f["row"].id}/versions',json=changed).status_code==409


@pytest.mark.parametrize('change',['amount','tax','currency','purpose','file','cancelled'])
def test_changes_invalidate_documentary_confirmation(flow,change):
    f=flow;_,payload=save(f);assert sign(f).status_code==200
    row=f['row'];db=f['db']
    if change=='file': Path(db.get(FileAsset,payload['payment_evidence']['file_id']).path_on_disk).write_bytes(b'changed')
    elif change=='purpose': row.payment_purpose='Changed purpose'
    elif change=='cancelled': row.status=MoneyMovementStatus.CANCELLED
    else: setattr(row,change,{'amount':101,'tax':11,'currency':'USD'}[change])
    db.commit()
    detail=f['api'](f['accountant']).get(f'{P}/movements/{row.id}').json()
    assert detail['review']['state'] in ('stale','cancelled')
    assert sign(f).status_code==409
    if change=='file': assert f['api'](f['accountant']).get(f'{P}/movements/{row.id}/documents/{payload["payment_evidence"]["file_id"]}').status_code==409


def test_permissions_missing_accountant_and_document_type(flow):
    f=flow;outsider=f['make_user'](Module.PRODUCTION)
    assert f['api'](outsider).get(P+'/overview').status_code==403
    viewer=f['make_user'](Module.ACCOUNTING,level=AccessLevel.VIEW)
    assert f['api'](viewer).get(P+'/overview').status_code==200
    file_id=document(f);payload=body(f,file_id)
    assert f['api'](viewer).post(f'{P}/movements/{f["row"].id}/versions',json=payload).status_code==403
    save(f,payload)
    f['db'].get(CostingApprovalPolicy,1).accounting_user_id=None;f['db'].commit()
    assert sign(f).status_code==403
    assert f['api'](f['owner']).post(f'{P}/movements/{f["row"].id}/documents',files={'file':('fake.pdf',b'not PDF','application/pdf')}).status_code==422


def test_unrecorded_old_file_does_not_gain_a_retroactive_attestation(flow):
    f=flow;db=f['db'];asset=save_bytes_file(db,'old.pdf','application/pdf',b'historical',FilePurpose.MONEY_MOVEMENT_DOCUMENT,f['accountant'])
    asset.sha256=None;asset.size_bytes=None;f['row'].documents=[asset];db.commit()
    assert f['api'](f['owner']).get(P+'/overview').json()['counts']['unclassified']==1
    assert f['api'](f['accountant']).post(f'{P}/movements/{f["row"].id}/versions',json=body(f,asset.id)).status_code==422


def test_filters_totals_and_cancellation_are_explicit(flow):
    f=flow;api=f['api'](f['owner'])
    assert api.get(P+'/overview?date_from=2026-12-01').json()['total']==0
    assert api.get(P+'/overview?date_from=2026-12-01&date_to=2026-11-01').status_code==422
    other=f['db'].query(BankAccount).filter(BankAccount.id!=f['row'].account_id).first()
    if other: assert api.get(P+f'/overview?account_id={other.id}').json()['total']==0
    assert api.get(P+'/overview?filter=to_pay').json()['total']==0
    f['row'].status=MoneyMovementStatus.DRAFT;f['db'].commit()
    document(f)
    assert f['api'](f['owner']).delete(f'/api/accounting/money-movements/{f["row"].id}').status_code==409
    assert api.get(P+'/overview?filter=to_pay').json()['total']==1


def test_advances_are_classified_without_becoming_costs_and_transfer_is_not_expense(flow):
    f=flow;payload=body(f,document(f));payload['nature']='advance';detail,_=save(f,payload)
    assert detail['versions'][0]['data']['nature']=='advance'
    assert sign(f).status_code==200
    assert f['api'](f['owner']).get(P+'/overview').json()['actual_profit_available'] is False
    payload=body(f,payload['payment_evidence']['file_id']);payload['nature']='transfer'
    assert f['api'](f['accountant']).post(f'{P}/movements/{f["row"].id}/versions',json=payload).status_code==422
    payload['allocations'][0]['category']='not_cost'
    assert save(f,payload)[0]['review']['state']=='pending'
