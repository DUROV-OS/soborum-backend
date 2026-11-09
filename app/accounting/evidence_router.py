from datetime import date
from io import BytesIO
from typing import Literal
from fastapi import APIRouter, Depends, Query, UploadFile
from sqlalchemy.orm import Session
from app.accounting import evidence as e
from app.accounting.evidence_models import PaymentEvidenceDocument as Document
from app.accounting.evidence_schemas import EvidenceInput, CheckInput, CATEGORIES
from app.common.files import FilePurpose, save_bytes_file, verified_file_response
from app.common.module_access import Module, AccessLevel
from app.core.deps import require_view, require_edit
from app.db.session import get_db
from app.users.models import UserRole
from app.costing.models import CostingEstimate

router=APIRouter(prefix='/evidence', tags=['Документальная проверка платежей'])
view=require_view(Module.ACCOUNTING)
edit=require_edit(Module.ACCOUNTING)


def editable(user):
    return user.role==UserRole.ADMIN or user.access_level(Module.ACCOUNTING)>=AccessLevel.EDIT


@router.get('/overview')
def overview(db:Session=Depends(get_db),user=Depends(view),organization_id:int|None=Query(None,gt=0),account_id:int|None=Query(None,gt=0),
             date_from:date|None=None,date_to:date|None=None,
             filter:Literal['all','attention','no_document','unclassified','pending','stale','verified','to_pay']='attention',
             offset:int=Query(0,ge=0),limit:int=Query(50,ge=1,le=100)):
    if date_from and date_to and date_from>date_to: e.fail('Начало периода позже окончания')
    return e.overview(db,organization_id,account_id,date_from,date_to,filter,offset,limit)


@router.get('/options')
def options(db:Session=Depends(get_db),user=Depends(view)):
    return {'categories':CATEGORIES,'projects':[{'id':p.id,'title':p.title,'number':p.number} for p in db.query(CostingEstimate).order_by(CostingEstimate.id.desc()).all()]}


@router.get('/movements/{id}')
def detail(id:int,db:Session=Depends(get_db),user=Depends(view)):
    return e.detail(db,e.movement(db,id),user,editable(user))


@router.post('/movements/{id}/versions',status_code=201)
def save(id:int,body:EvidenceInput,db:Session=Depends(get_db),user=Depends(edit)):
    row=e.movement(db,id,True);e.save_version(db,row,body,user);db.commit()
    return e.detail(db,row,user,True)


@router.post('/movements/{id}/check')
def check(id:int,body:CheckInput,db:Session=Depends(get_db),user=Depends(edit)):
    from app.costing.service import lock
    lock(db)
    row=e.movement(db,id,True);e.sign(db,row,body,user);db.commit()
    return e.detail(db,row,user,True)


@router.post('/movements/{id}/documents',status_code=201)
async def upload(id:int,file:UploadFile,db:Session=Depends(get_db),user=Depends(edit)):
    row=e.movement(db,id,True)
    if row.status.value=='cancelled': e.fail('Платёж отменён')
    raw=await file.read(20*1024*1024+1)
    if not raw or len(raw)>20*1024*1024: e.fail('Загрузите PDF или скан до 20 МБ')
    try:
        if raw.startswith(b'%PDF-'):
            from pypdf import PdfReader
            pdf=PdfReader(BytesIO(raw))
            if pdf.is_encrypted or len(pdf.pages)==0: raise ValueError()
            content_type='application/pdf'
        else:
            from PIL import Image
            with Image.open(BytesIO(raw)) as image:
                if image.format not in ('PNG','JPEG','WEBP') or image.width*image.height>40_000_000: raise ValueError()
                content_type=Image.MIME[image.format];image.verify()
    except Exception: e.fail('Файл не распознан как PDF или скан PNG/JPEG/WEBP')
    filename=(file.filename or 'Документ').replace('\\','/').split('/')[-1][:255]
    asset=save_bytes_file(db,filename,content_type,raw,FilePurpose.MONEY_MOVEMENT_DOCUMENT,user)
    db.add(Document(movement_id=id,file_id=asset.id,actor_id=user.id));db.commit()
    return {'file_id':asset.id,'filename':asset.filename,'sha256':asset.sha256}


@router.get('/movements/{id}/documents/{file_id}')
def download(id:int,file_id:int,db:Session=Depends(get_db),user=Depends(view)):
    asset=e.documents(db,e.movement(db,id)).get(file_id)
    if not asset: e.fail('Документ этого платежа не найден',404)
    return verified_file_response(asset)
