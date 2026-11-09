from datetime import datetime
from decimal import Decimal
from io import BytesIO
from uuid import UUID
from fastapi import APIRouter, Depends, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session
from app.common.files import FileAsset, FilePurpose, save_bytes_file, verified_file_response
from app.common.module_access import Module, AccessLevel
from app.core.deps import get_current_user, require_edit, require_view
from app.db.session import get_db
from app.warehouse import controls as c, service as s
from app.warehouse.control_models import WarehouseControlDocument as Document, WarehouseControlRecord as Record
from app.warehouse.models import StockMovement, StockMovementReason, WarehouseMaterial

router=APIRouter(prefix='/control',tags=['Контроль выдачи и возвратов'])


class Handoff(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
    request_id: UUID
    source_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    file_id: int = Field(gt=0)
    locator: str = Field(min_length=3,max_length=500)


class Return(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
    request_id: UUID
    quantity: Decimal = Field(gt=0,le=1000000000,decimal_places=6,allow_inf_nan=False)
    file_id: int = Field(gt=0)
    locator: str = Field(min_length=3,max_length=500)
    reason: str = Field(min_length=3,max_length=400)
    received_by: str = Field(min_length=1,max_length=255)
    occurred_at: datetime | None = None


def reader(user=Depends(get_current_user)):
    if max(user.access_level(Module.WAREHOUSE),user.access_level(Module.PRODUCTION))<AccessLevel.VIEW:c.fail('Нет доступа к складу или производству',403)
    return user


def editor(user=Depends(get_current_user)):
    if max(user.access_level(Module.WAREHOUSE),user.access_level(Module.PRODUCTION))<AccessLevel.EDIT:c.fail('Нет прав на документы склада или производства',403)
    return user


@router.get('/houses/{production_id}')
def house(production_id:int,db:Session=Depends(get_db),user=Depends(reader)):
    return c.state(db,production_id,user)


@router.post('/houses/{production_id}/handoff')
def handoff(production_id:int,body:Handoff,db:Session=Depends(get_db),user=Depends(require_edit(Module.PRODUCTION))):
    c.handoff(db,production_id,body,user);db.commit();return c.state(db,production_id,user)


@router.get('/queue')
def queue(db:Session=Depends(get_db),user=Depends(require_view(Module.WAREHOUSE))):
    rows=[]
    for house in s.techcard_houses(db,all_houses=True):
        pid=house['production_id'];preview=s.techcard_preview(db,s.get_production_or_404(db,pid));status=c.state(db,pid,user)
        lines=preview['lines']
        rows.append({**house,'control':status,
            'remaining_positions':sum(1 for l in lines if l['to_issue']>0 or l['requested']>0),
            'available_positions':sum(1 for l in lines if l['to_issue']>0 and l['available_quantity']>0),
            'shortage_positions':sum(1 for l in lines if l['to_issue']+l['requested']>l['available_quantity']),
            'requested_positions':sum(1 for l in lines if l['requested']>0),
            'provided_positions':sum(1 for l in lines if l['provided']>0)})
    anomalies=[]
    materials=db.query(WarehouseMaterial).filter_by(is_archived=False).order_by(WarehouseMaterial.id).all()
    holds=s.reservations.allocations(db,[m.id for m in materials])
    for m in materials:
        available=Decimal(str(m.quantity_in_stock))-sum(holds.get(m.id,{}).values(),Decimal(0))
        if m.quantity_in_stock<0 or available<0:
            anomalies.append({'material_id':m.id,'title':m.title,'stock':m.quantity_in_stock,'available':available,'unit':m.unit})
    return {'houses':rows,'stock_anomalies':anomalies,'can_edit':user.access_level(Module.WAREHOUSE)>=AccessLevel.EDIT}


@router.get('/returns')
def returns(db:Session=Depends(get_db),user=Depends(require_view(Module.WAREHOUSE))):
    movements=db.query(StockMovement).filter(StockMovement.reason.in_([StockMovementReason.ISSUED,StockMovementReason.ISSUED_TECHCARD]),StockMovement.delta<0).order_by(StockMovement.id.desc()).all()
    productions=s.productions_of_movements(db,movements);rows=[]
    issued_records=db.query(Record).filter(Record.kind.in_(["issue","request_issue"])).order_by(Record.id).all()
    returns_by_movement={}
    for row in db.query(Record).filter_by(kind="return").order_by(Record.id):
        returns_by_movement.setdefault(row.content["movement_id"],[]).append(row)
    for m in movements:
        production=productions.get(m.id)
        if not production:continue
        reason=None
        try:c.original_allocations(db,m,issued_records)
        except Exception as exc:
            from fastapi import HTTPException
            if not isinstance(exc,HTTPException):raise
            reason=exc.detail
        returned_rows=returns_by_movement.get(m.id,[])
        returned=sum((Decimal(r.content['request']['quantity']) for r in returned_rows),Decimal(0))
        rows.append({'movement_id':m.id,'operation_id':m.operation_id,'production_id':production.id,'house':s.house_label(production),
            'material':m.warehouse_material.title,'unit':m.warehouse_material.unit,'fractional':m.warehouse_material.is_fractional,
            'issued':c.number(-Decimal(str(m.delta))),'returned':c.number(returned),'remaining':c.number(-Decimal(str(m.delta))-returned),
            'occurred_at':m.operation.occurred_at if m.operation else m.created_at,'blocked_reason':reason,'returns':c.return_facts(db,m.id,returned_rows)})
    return rows


@router.post('/returns/{movement_id}')
def return_material(movement_id:int,body:Return,db:Session=Depends(get_db),user=Depends(require_edit(Module.WAREHOUSE))):
    op=c.return_material(db,movement_id,body,user);db.commit();return {'operation_id':op.id}


@router.post('/documents',status_code=201)
async def upload(file:UploadFile,db:Session=Depends(get_db),user=Depends(editor)):
    raw=await file.read(20*1024*1024+1)
    if not raw or len(raw)>20*1024*1024:c.fail('Загрузите PDF или скан до 20 МБ')
    try:
        if raw.startswith(b'%PDF-'):
            from pypdf import PdfReader
            pdf=PdfReader(BytesIO(raw))
            if pdf.is_encrypted or not pdf.pages:raise ValueError()
            content_type='application/pdf'
        else:
            from PIL import Image
            with Image.open(BytesIO(raw)) as image:
                if image.format not in ('PNG','JPEG','WEBP') or image.width*image.height>40_000_000:raise ValueError()
                content_type=Image.MIME[image.format];image.verify()
    except Exception:c.fail('Файл не распознан как PDF или скан PNG/JPEG/WEBP')
    filename=(file.filename or 'Документ склада').replace('\\','/').split('/')[-1][:255]
    asset=save_bytes_file(db,filename,content_type,raw,FilePurpose.COSTING_EVIDENCE,user)
    db.add(Document(file_id=asset.id,actor_id=user.id));db.commit()
    return {'file_id':asset.id,'filename':asset.filename,'sha256':asset.sha256}


@router.get('/documents/{file_id}')
def download(file_id:int,db:Session=Depends(get_db),user=Depends(reader)):
    doc=db.query(Document).filter_by(file_id=file_id).first()
    asset=db.get(FileAsset,file_id) if doc else None
    if not asset:c.fail('Документ склада не найден',404)
    return verified_file_response(asset)
