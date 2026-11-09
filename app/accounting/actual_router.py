from io import BytesIO
from decimal import Decimal
from fastapi import APIRouter, Depends, Query, UploadFile
from sqlalchemy.orm import Session
from app.accounting import actual as a
from app.accounting.actual_models import ActualCostDocument as Document
from app.accounting.actual_schemas import CostInput, CostDecision, ClosureInput, DirectorClosure, CATEGORIES
from app.accounting.evidence_router import editable
from app.accounting.models import Organization
from app.common.files import FileAsset, FilePurpose, save_bytes_file, verified_file_response
from app.common.module_access import Module
from app.core.deps import require_view, require_edit
from app.db.session import get_db
from app.costing.governance import responsible
from app.costing.models import CostingEstimate
from app.costing.service import lock
from app.purchasing.models import PurchaseReceipt
from app.warehouse.models import StockMovement, StockMovementReason
from app.warehouse.service import productions_of_movements
from app.production.models import Production
from app.sales.models import SalesShipment

router=APIRouter(prefix='/actual-costs',tags=['Фактические затраты проектов'])
view=require_view(Module.ACCOUNTING)
edit=require_edit(Module.ACCOUNTING)


@router.get('')
def overview(db:Session=Depends(get_db),user=Depends(view),project_id:int|None=Query(None,gt=0)):
    result=a.overview(db,project_id)
    person=responsible(db,'accounting')
    return {**result,'can_edit':editable(user),'can_check':editable(user) and person is not None and person.id==user.id,
            'can_approve':editable(user) and getattr(responsible(db,'director'),'id',None)==user.id,
            'responsible':{'id':person.id,'name':person.full_name} if person else None}


@router.get('/options')
def options(db:Session=Depends(get_db),user=Depends(view)):
    documents=db.query(FileAsset).join(Document,Document.file_id==FileAsset.id).order_by(Document.id.desc()).all()
    movements=db.query(StockMovement).filter(StockMovement.reason.in_([StockMovementReason.ISSUED,StockMovementReason.ISSUED_TECHCARD,StockMovementReason.ISSUED_MANUAL]),StockMovement.delta<0).order_by(StockMovement.id.desc()).all()
    productions=productions_of_movements(db,movements)
    return {'categories':CATEGORIES,
        'projects':[{'id':p.id,'title':p.title,'number':p.number} for p in db.query(CostingEstimate).filter_by(archived=False).order_by(CostingEstimate.id.desc())],
        'organizations':[{'id':o.id,'name':o.name} for o in db.query(Organization).order_by(Organization.id)],
        'productions':[{'id':p.id,'name':p.name,'client_id':p.cycle.client.id if p.cycle.client else None} for p in db.query(Production).order_by(Production.id)],
        'shipments':[{'id':s.id,'number':s.number,'order_id':s.order_id,'amount':str(s.total)} for s in db.query(SalesShipment).filter_by(status='posted',archived=False).order_by(SalesShipment.id)],
        'documents':[{'id':f.id,'filename':f.filename,'sha256':f.sha256,'integrity':a.checked_file(f)} for f in documents],
        'receipts':[{'id':r.id,'number':r.number,'date':r.occurred_at,'lines':r.lines} for r in db.query(PurchaseReceipt).filter_by(status='posted').filter(PurchaseReceipt.operation_id.isnot(None)).order_by(PurchaseReceipt.id.desc())],
        'movements':[{'id':m.id,'material_id':m.warehouse_material_id,'title':m.warehouse_material.title,'quantity':str(-a.D(str(m.delta))-a.returned_quantity(db,m.id)),
                      'production':productions[m.id].name,'client_id':productions[m.id].cycle.client.id if productions[m.id].cycle.client else None,
                      'date':str(a.local_date(m.operation.occurred_at if m.operation else m.created_at))} for m in movements if m.id in productions]}


@router.get('/records/{id}')
def detail(id:int,db:Session=Depends(get_db),user=Depends(view)):
    return a.detail(db,id)


@router.get('/material-valuation')
def valuation(receipt_id:int=Query(gt=0),receipt_line:int=Query(ge=0),quantity:Decimal=Query(gt=0,le=10000000,max_digits=17,decimal_places=6,allow_inf_nan=False),record_id:int|None=Query(None,gt=0),db:Session=Depends(get_db),user=Depends(view)):
    return a.material_value(db,receipt_id,receipt_line,quantity,record_id)


@router.post('/records',status_code=201)
def create(body:CostInput,db:Session=Depends(get_db),user=Depends(edit)):
    lock(db);v=a.save(db,body,user);db.commit();return a.detail(db,v.record_id)


@router.post('/records/{id}/versions',status_code=201)
def version(id:int,body:CostInput,db:Session=Depends(get_db),user=Depends(edit)):
    lock(db);v=a.save(db,body,user,id);db.commit();return a.detail(db,v.record_id)


@router.post('/records/{id}/decisions')
def decision(id:int,body:CostDecision,db:Session=Depends(get_db),user=Depends(edit)):
    lock(db);a.decide(db,id,body,user);db.commit();return a.detail(db,id)


@router.post('/projects/{id}/accounting-close')
def accounting_close(id:int,body:ClosureInput,db:Session=Depends(get_db),user=Depends(edit)):
    from app.accounting.actual_closure import close_accounting
    lock(db);close_accounting(db,id,body,user);db.commit();return a.overview(db,id)['projects'][0]


@router.post('/projects/{id}/director-close')
def director_close(id:int,body:DirectorClosure,db:Session=Depends(get_db),user=Depends(edit)):
    from app.accounting.actual_closure import close_director
    lock(db);close_director(db,id,body,user);db.commit();return a.overview(db,id)['projects'][0]


@router.post('/documents',status_code=201)
async def upload(file:UploadFile,db:Session=Depends(get_db),user=Depends(edit)):
    raw=await file.read(20*1024*1024+1)
    if not raw or len(raw)>20*1024*1024: a.fail('Загрузите PDF или скан до 20 МБ')
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
    except Exception: a.fail('Файл не распознан как PDF или скан PNG/JPEG/WEBP')
    filename=(file.filename or 'Документ затрат').replace('\\','/').split('/')[-1][:255]
    asset=save_bytes_file(db,filename,content_type,raw,FilePurpose.COSTING_EVIDENCE,user)
    db.add(Document(file_id=asset.id,actor_id=user.id));db.commit()
    return {'file_id':asset.id,'filename':asset.filename,'sha256':asset.sha256}


@router.get('/documents/{file_id}')
def download(file_id:int,db:Session=Depends(get_db),user=Depends(view)):
    doc=db.query(Document).filter_by(file_id=file_id).first()
    asset=db.get(FileAsset,file_id) if doc else None
    if not asset: a.fail('Документ затрат не найден',404)
    return verified_file_response(asset)
