from datetime import date
from typing import Literal
from uuid import UUID, uuid5
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import Field
from sqlalchemy.orm import Session
from app.core.deps import get_current_user
from app.db.session import get_db
from app.accounting.evidence_schemas import Strict, Citation
from app.accounting.models import Counterparty
from . import registry as listing, service as s, sync
from app.common.files import verified_file_response
from .models import Change, Preview
from .schemas import Save, Apply
from .documents import documents, export

def private_response(response:Response):
    response.headers['Cache-Control']='private, no-store'


router=APIRouter(prefix='/api/counterparties',tags=['counterparties'],dependencies=[Depends(private_response)])


class Verify(Strict):
    request_id:UUID
    etag:str
    evidence:Citation


@router.post('/{id}/verify-bank')
def verify_bank(id:int,body:Verify,db:Session=Depends(get_db),user=Depends(get_current_user)):
    from app.costing import governance
    from app.purchasing.controls import proofs
    from .verification import basis
    from .schemas import validate
    governance.require_person(db,user,'accounting');s.lock(db)
    request={'id':id,'verify':body.model_dump(mode='json')}
    old=s.replay(db,user,body.request_id,request)
    if old:return old
    cp=s.get(db,id,user,True);before=s.out(db,cp,user)
    if before['etag']!=body.etag:raise HTTPException(409,'Реквизиты изменились. Проверьте новую версию.')
    if not cp.supplier_id:raise HTTPException(422,'Документальная проверка доступна для закупочного профиля')
    validate(before['card'])
    if not any(not r['archived'] for r in before['card']['accounts']):raise HTTPException(422,'Добавьте действующий банковский счёт')
    evidence=proofs(db,cp.supplier_id,[body.evidence])
    db.add(Change(counterparty_id=id,actor_id=user.id,request_id=str(body.request_id),request_hash=s.digest(request),
                  data={'kind':'bank_verification','before':before,'after':before,'basis':basis(db,cp),'proofs':evidence,'source':{'kind':'document','evidence':body.evidence.model_dump()}}))
    db.commit();return s.out(db,cp,user)


@router.get('')
def registry(
    q: str = Query('', max_length=255),
    name: str = Query('', max_length=255),
    inn: str = Query('', max_length=32),
    kpp: str = Query('', max_length=9),
    phone: str = Query('', max_length=100),
    status: list[listing.RegistryStatus] = Query(default=[]),
    comment: str = Query('', max_length=255),
    sort: listing.Sort = 'name',
    direction: Literal['asc', 'desc'] = 'asc',
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    return listing.list_rows(
        db, user, q=q, name=name, inn=inn, kpp=kpp, phone=phone, statuses=set(status),
        comment=comment, sort=sort, direction=direction, offset=offset, limit=limit,
    )


@router.get('/registry/profiles')
def registry_profiles(role: listing.Role, q: str = Query('', max_length=255), db: Session = Depends(get_db), user=Depends(get_current_user)):
    return listing.available_profiles(db, user, role, q)


@router.patch('/{id}/registry')
def edit_registry(id: int, body: listing.RegistryEdit, db: Session = Depends(get_db), user=Depends(get_current_user)):
    return listing.edit(db, user, id, body)


@router.get('/{id}/history/{change_id}/document')
def history_document(id: int, change_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    asset = listing.history_asset(db, user, s.get(db, id, user), change_id)
    return verified_file_response(asset)


@router.post('',status_code=201)
def create(body:Save,db:Session=Depends(get_db),user=Depends(get_current_user)):
    return s.save(db,user,body)


class Batch(Strict):
    ids:list[int]=Field(min_length=1,max_length=20)


class BatchApply(Strict):
    request_id:UUID
    previews:list[UUID]=Field(min_length=1,max_length=20)


@router.post('/backfill/preview')
def batch_preview(body:Batch,db:Session=Depends(get_db),user=Depends(get_current_user)):
    result=[]
    for id in dict.fromkeys(body.ids):
        try: result.append({'id':id,'preview':sync.preview(db,user,id)})
        except HTTPException as e:db.rollback();result.append({'id':id,'error':e.detail})
    return {'items':result}


@router.post('/backfill/apply')
def batch_apply(body:BatchApply,db:Session=Depends(get_db),user=Depends(get_current_user)):
    result=[]
    for pid in body.previews:
        row=db.get(Preview,str(pid))
        if not row or row.actor_id!=user.id:raise HTTPException(404,'Сравнение не найдено')
    for pid in dict.fromkeys(body.previews):
        row=db.get(Preview,str(pid))
        if not row or row.actor_id!=user.id:raise HTTPException(404,'Сравнение не найдено')
        try:
            fields=[c['key'] for c in row.data['changes'] if c['fill_empty']]
            if not fields:result.append({'id':row.counterparty_id,'status':'unchanged'});continue
            sync.apply(db,user,row.counterparty_id,Apply(request_id=uuid5(body.request_id,str(pid)),preview_id=pid,fields=fields))
            result.append({'id':row.counterparty_id,'status':'updated'})
        except HTTPException as e:db.rollback();result.append({'id':row.counterparty_id,'status':'error','error':e.detail})
    return {'items':result}


@router.get('/by-supplier/{id}')
def by_supplier(id:int,db:Session=Depends(get_db),user=Depends(get_current_user)):
    rows=db.query(Counterparty).filter_by(supplier_id=id).all()
    if len(rows)!=1:raise HTTPException(409,'Связь поставщика с контрагентом требует проверки')
    return s.out(db,s.get(db,rows[0].id,user),user)


@router.get('/{id}')
def get(id:int,db:Session=Depends(get_db),user=Depends(get_current_user)):
    return s.out(db,s.get(db,id,user),user)


@router.patch('/{id}')
def save(id:int,body:Save,db:Session=Depends(get_db),user=Depends(get_current_user)):
    return s.save(db,user,body,id)


@router.get('/{id}/history')
def history(id:int,offset:int=Query(0,ge=0),db:Session=Depends(get_db),user=Depends(get_current_user)):
    s.get(db,id,user)
    rows=db.query(Change).filter_by(counterparty_id=id).order_by(Change.id.desc()).offset(offset).limit(50)
    return [{'id':r.id,'actor_id':r.actor_id,'created_at':r.created_at,**listing.visible_change(user, r.data)} for r in rows]


@router.post('/{id}/moysklad/preview')
def preview(id:int,db:Session=Depends(get_db),user=Depends(get_current_user)):
    return sync.preview(db,user,id)


@router.post('/{id}/moysklad/apply')
def apply(id:int,body:Apply,db:Session=Depends(get_db),user=Depends(get_current_user)):
    return sync.apply(db,user,id,body)


@router.get('/{id}/documents')
def docs(id:int,types:str='',date_from:date|None=None,date_to:date|None=None,offset:int=Query(0,ge=0),limit:int=Query(50,ge=1,le=100),db:Session=Depends(get_db),user=Depends(get_current_user)):
    rows,warnings=documents(db,user,s.get(db,id,user),types,date_from,date_to)
    return {'items':rows[offset:offset+limit],'total':len(rows),'warnings':warnings}


@router.get('/{id}/documents/export')
def download(id:int,format:Literal['csv','xlsx']='csv',types:str='',date_from:date|None=None,date_to:date|None=None,db:Session=Depends(get_db),user=Depends(get_current_user)):
    rows,_=documents(db,user,s.get(db,id,user),types,date_from,date_to)
    data,mime=export(rows,format)
    return Response(data,media_type=mime,headers={'Content-Disposition':f'attachment; filename="counterparty-{id}.{format}"','Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff'})
