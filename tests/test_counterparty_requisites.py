from uuid import uuid4
from datetime import datetime, timezone
from io import BytesIO
import pytest
from app.common.module_access import Module, AccessLevel
from app.accounting.models import Counterparty, CounterpartyKind
from app.counterparties.models import Change, Details, Account
from app.counterparties.schemas import Card, valid_inn
from app.moysklad.models import SourceLink, SourceRecord

P='/api/counterparties'


def payload(**values):
    return {'request_id':str(uuid4()),'card':Card(name='Тестовый поставщик',inn='7707083893',legal_type='legal_entity',**values).model_dump(mode='json')}


def create(c,**values):
    response=c.post(P,json=payload(**values));assert response.status_code==201,response.text
    return response.json()


def save(c,row,**changes):
    return c.patch(P+'/'+str(row['id']),json={'request_id':str(uuid4()),'etag':row['etag'],'card':{**row['card'],**changes}})


def test_form_validation_and_legal_type(api,make_user):
    c=api(make_user(Module.WAREHOUSE));row=create(c,kpp='773601001')
    assert valid_inn('7707083893') and not valid_inn('7707083894')
    assert save(c,row,inn='7707083894').status_code==422
    assert save(c,row,legal_type='individual').status_code==422
    assert save(c,row,addresses_same=True,legal_address='А',actual_address='Б').status_code==422
    assert c.post(P,json=payload()).status_code==409
    data=payload();data['card']['inn']='';assert c.post(P,json=data).status_code==422


def test_optimistic_edit_and_idempotent_create(api,make_user,db):
    c=api(make_user(Module.WAREHOUSE));body=payload();row=c.post(P,json=body).json()
    assert c.post(P,json=body).json()['id']==row['id']
    assert db.query(Counterparty).count()==1
    result=save(c,row,legal_address='Москва');assert result.status_code==200
    assert save(c,row,actual_address='Казань').status_code==409
    assert db.query(Change).count()==2
    assert c.get(P+f'/{row["id"]}/history').json()[0]['before']['card']['legal_address']==''


def test_legacy_mutation_invalidates_etag_and_incomplete_cards_remain_editable(api,make_user,db):
    user=make_user(Module.ACCOUNTING);c=api(user)
    cp=Counterparty(name='Старый',kind=CounterpartyKind.OTHER);db.add(cp);db.commit()
    row=c.get(P+f'/{cp.id}').json()
    assert save(c,row,actual_address='Москва').status_code==200
    row=c.get(P+f'/{cp.id}').json();cp.name='Из бухгалтерии';db.commit()
    assert save(c,row,actual_address='Казань').status_code==409


def test_permissions_no_finance_leak(api,make_user,db):
    admin=make_user(admin=True);row=create(api(admin))
    other=Counterparty(name='Налоговый орган',kind=CounterpartyKind.GOVERNMENT);db.add(other);db.commit()
    c=api(make_user(Module.WAREHOUSE,level=AccessLevel.VIEW))
    assert c.get(P).json()['total']==1
    assert c.get(P+f'/{other.id}').status_code==404
    assert save(c,row,name='Изменение').status_code==403
    assert c.get(P+f'/{row["id"]}/documents').json()['items']==[]
    assert api(make_user()).get(P).status_code==403


def source(db,id,**fields):
    sid=str(uuid4())
    db.add(SourceLink(source_key='counterparty/'+sid,target_type='counterparties',target_id=id))
    row=SourceRecord(entity_type='counterparty',source_id=sid,title='Источник',search_text='',payload={'id':sid,'inn':'7707083893','companyType':'legal','accounts':{'rows':[]},'contactpersons':{'rows':[]},**fields})
    db.add(row);db.commit();return row


def test_sync_preserves_values_and_rejects_changed_source(api,make_user,db):
    c=api(make_user(Module.ACCOUNTING));row=create(c,legal_address='Наш адрес')
    remote=source(db,row['id'],legalAddress='Иной адрес',actualAddress='Новый адрес')
    preview=c.post(P+f'/{row["id"]}/moysklad/preview').json()
    changes={r['key']:r for r in preview['changes']}
    assert changes['legal_address']['fill_empty'] is False
    assert changes['actual_address']['fill_empty'] is True
    body={'request_id':str(uuid4()),'preview_id':preview['id'],'fields':['actual_address']}
    result=c.post(P+f'/{row["id"]}/moysklad/apply',json=body)
    assert result.status_code==200,result.text
    assert result.json()['card']['legal_address']=='Наш адрес'
    assert c.post(P+f'/{row["id"]}/moysklad/apply',json=body).status_code==200
    preview=c.post(P+f'/{row["id"]}/moysklad/preview').json()
    remote.payload={**remote.payload,'legalAddress':'Изменился'};db.commit()
    assert c.post(P+f'/{row["id"]}/moysklad/apply',json={'request_id':str(uuid4()),'preview_id':preview['id'],'fields':['legal_address']}).status_code==409


def test_accounts_validation_archive_and_cross_card_ids(api,make_user,db):
    c=api(make_user(Module.ACCOUNTING));row=create(c)
    bank={'id':str(uuid4()),'account_number':'40702810900000002859','bik':'044525225','bank_name':'Банк','correspondent_account':'30101810400000000225','bank_address':'','primary':True,'archived':False}
    # Build a valid control digit for this synthetic test account.
    for digit in '0123456789':
        bank['account_number']=bank['account_number'][:8]+digit+bank['account_number'][9:]
        r=save(c,row,accounts=[bank])
        if r.status_code==200:break
    assert r.status_code==200,r.text
    newer=r.json();assert not newer['bank_verification']['verified']
    assert save(c,newer,accounts=[]).status_code==200
    assert db.query(Account).one().data['archived'] is True


def test_document_export_actual_currency_and_formula_safety(api,make_user,db):
    c=api(make_user(Module.ACCOUNTING));row=create(c);remote=source(db,row['id'])
    db.add(SourceRecord(entity_type='facturein',source_id=str(uuid4()),title='=1+1',search_text='',state='Проведён',payload={'agent':{'id':remote.source_id},'name':'=1+1','moment':'2026-11-18 12:00:00','sum':10050}));db.commit()
    docs=c.get(P+f'/{row["id"]}/documents',params={'types':'facturein'}).json()
    assert docs['total']==1 and docs['items'][0]['amount']=='100.50' and docs['items'][0]['currency']==''
    csv=c.get(P+f'/{row["id"]}/documents/export').content.decode('utf-8-sig');assert "'=1+1" in csv
    from openpyxl import load_workbook
    book=load_workbook(BytesIO(c.get(P+f'/{row["id"]}/documents/export?format=xlsx').content))
    assert book.active['B2'].data_type!='f'
    assert c.get(P+f'/{row["id"]}/documents?date_from=2026-12-01&date_to=2026-11-01').status_code==422


def test_batch_only_fills_empty_fields(api,make_user,db):
    c=api(make_user(Module.ACCOUNTING));row=create(c,legal_address='Сохранить')
    source(db,row['id'],legalAddress='Заменить',actualAddress='Заполнить')
    preview=c.post(P+'/backfill/preview',json={'ids':[row['id']]}).json()['items'][0]['preview']
    result=c.post(P+'/backfill/apply',json={'request_id':str(uuid4()),'previews':[preview['id']]}).json()
    assert result['items'][0]['status']=='updated',result
    card=c.get(P+f'/{row["id"]}').json()['card'];assert card['legal_address']=='Сохранить' and card['actual_address']=='Заполнить'


def test_old_supplier_contacts_survive_and_status_tracks_legacy_profile(api,make_user,db):
    from app.warehouse import service
    from app.warehouse.schemas import SupplierCreate, SupplierUpdate
    from fastapi import HTTPException
    supplier=service.create_supplier(db,SupplierCreate(name='Старый',contacts=[{'kind':'phone','value':'+7 999 111 22 33','person':'Иван'}]));db.commit()
    c=api(make_user(Module.WAREHOUSE));row=c.get(P+'/by-supplier/'+str(supplier.id)).json()
    assert row['card']['contacts'][0]['name']=='Иван'
    assert save(c,row,actual_address='Москва').status_code==200
    assert c.get(P+f'/{row["id"]}').json()['card']['contacts']==row['card']['contacts']
    service.update_supplier(db,supplier,SupplierUpdate(status='archived'));db.commit()
    assert not c.get(P+f'/{row["id"]}').json()['active']
    with pytest.raises(HTTPException) as error:service.delete_supplier(db,supplier)
    assert error.value.status_code==409


def test_bank_confirmation_is_personal_document_based_and_invalidated(api,make_user,db,tmp_path,monkeypatch):
    from pathlib import Path
    from app.common import files
    from app.costing.governance_models import CostingApprovalPolicy
    from app.counterparties.verification import ready_for_supplier
    from tests.test_purchase_controls import upload
    monkeypatch.setattr(files.settings,'storage_dir',str(tmp_path))
    accountant=make_user(Module.ACCOUNTING);other=make_user(admin=True)
    db.add(CostingApprovalPolicy(id=1,accounting_user_id=accountant.id));db.commit()
    c=api(accountant)
    number='40702810000000002859';bik='044525225'
    number=next(number[:8]+digit+number[9:] for digit in '0123456789' if sum(int(n)*[7,1,3][i%3] for i,n in enumerate(bik[-3:]+number[:8]+digit+number[9:]))%10==0)
    row=create(c,accounts=[{'account_number':number,'bik':bik,'bank_name':'Банк'}])
    assert not ready_for_supplier(db,row['supplier_id'])
    proof=upload(c,row['supplier_id']);body={'request_id':str(uuid4()),'etag':row['etag'],'evidence':proof}
    assert api(other).post(P+f'/{row["id"]}/verify-bank',json=body).status_code==403
    c=api(accountant);result=c.post(P+f'/{row["id"]}/verify-bank',json=body)
    assert result.status_code==200,result.text
    assert result.json()['bank_verification']['verified'] and ready_for_supplier(db,row['supplier_id'])
    result=save(c,result.json(),accounts=[{**result.json()['card']['accounts'][0],'bank_name':'Изменённый банк'}])
    assert result.status_code==200 and not result.json()['bank_verification']['verified']
    assert not ready_for_supplier(db,row['supplier_id'])
    body={**body,'request_id':str(uuid4()),'etag':result.json()['etag']}
    assert c.post(P+f'/{row["id"]}/verify-bank',json=body).json()['bank_verification']['verified']
    Path(db.get(files.FileAsset,proof['file_id']).path_on_disk).write_bytes(b'changed')
    assert not c.get(P+f'/{row["id"]}').json()['bank_verification']['verified']


def test_live_mcp_pagination_bic_mapping_and_partial_failure(monkeypatch):
    from app.counterparties import sync
    from app.core import mcp_remote
    from fastapi import HTTPException
    sid=str(uuid4());calls=[]
    monkeypatch.setattr(sync.settings,'moysklad_token','')
    monkeypatch.setattr(sync.settings,'moysklad_mcp_url','https://test.invalid')
    monkeypatch.setattr(sync.settings,'moysklad_mcp_client_id','isolated')
    monkeypatch.setattr(sync.settings,'moysklad_mcp_client_secret','isolated')
    class Remote:
        def call_tool(self,name,args,**kwargs):
            calls.append(args)
            if args['path'].endswith('/accounts'):
                return {'rows':[{'id':str(i),'bic':'044525225','accountNumber':'123'} for i in range(100)] if args['params']['offset']==0 else []}
            if args['path'].endswith('/contactpersons'):return {'rows':[]}
            return {'id':sid,'companyType':'legal'}
    monkeypatch.setattr(mcp_remote,'client_for',lambda target:Remote())
    raw=sync.fetch_live(sid)
    assert len(raw['accounts']['rows'])==100 and len(calls)==4
    data,_=sync.mapped(raw,Card().model_dump(mode='json'))
    assert data['accounts'][0]['bik']=='044525225'
    class Broken(Remote):
        def call_tool(self,name,args,**kwargs):
            if args['path'].endswith('/contactpersons'):raise mcp_remote.McpError('Unavailable')
            return super().call_tool(name,args,**kwargs)
    monkeypatch.setattr(mcp_remote,'client_for',lambda target:Broken())
    with pytest.raises(HTTPException) as error:sync.fetch_live(sid)
    assert error.value.status_code==503
