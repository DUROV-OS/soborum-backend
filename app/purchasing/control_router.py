from io import BytesIO
from fastapi import APIRouter, Depends, UploadFile
from sqlalchemy.orm import Session
from app.common.module_access import Module
from app.core.deps import require_view, require_edit
from app.common.files import FilePurpose, save_bytes_file, verified_file_response
from app.db.session import get_db
from app.purchasing import controls as c, service
from app.purchasing.control_models import PurchaseControlDocument as Document, PurchaseControlRecord as Record
from app.purchasing.control_schemas import Trust, Price, Offer, Approve
from app.users.models import User

router = APIRouter()
view = require_view(Module.ACCOUNTING)
edit = require_edit(Module.ACCOUNTING)


@router.get('/orders/{id}/automatic-review')
def review(id: int, db: Session = Depends(get_db), user=Depends(view)):
    return c.review(db, service.get_order(db, id), user)


@router.get('/orders/{id}/control-history')
def history(id: int, db: Session = Depends(get_db), user=Depends(view)):
    row = service.get_order(db, id)
    events = db.query(Record).filter(Record.supplier_id == row.supplier_id,
        (Record.order_id == id) | Record.order_id.is_(None)).order_by(Record.id.desc()).limit(100)
    return [{'id': r.id, 'kind': r.kind, 'created_at': r.created_at,
             'actor': db.get(User, r.actor_id).full_name, 'content': r.content} for r in events]


@router.post('/suppliers/{id}/trust')
def trust(id: int, body: Trust, db: Session = Depends(get_db), user=Depends(edit)):
    row = c.trust(db, id, body, user); db.commit()
    return {'id': row.id}


@router.post('/orders/{id}/price-evidence')
def price(id: int, body: Price, db: Session = Depends(get_db), user=Depends(edit)):
    c.lock(db)
    row = service.get_order(db, id, lock=True)
    event = c.price(db, row, body, user); db.commit()
    return {'id': event.id}


@router.post('/suppliers/{id}/offers')
def offer(id: int, body: Offer, db: Session = Depends(get_db), user=Depends(edit)):
    event = c.offer(db, id, body, user); db.commit()
    return {'id': event.id}


@router.post('/orders/{id}/automatic-approval')
def approve(id: int, body: Approve, db: Session = Depends(get_db), user=Depends(edit)):
    c.lock(db)
    row = service.get_order(db, id, lock=True)
    event = c.approve(db, row, body, user); db.commit()
    return {'id': event.id, 'review': c.review(db, row, user)}


@router.get('/suppliers/{id}/documents')
def documents(id: int, db: Session = Depends(get_db), user=Depends(view)):
    c.supplier(db, id)
    return [{'id': f.id, 'filename': f.filename} for f in c.documents(db, id).values()]


@router.get('/suppliers/{id}/documents/{file_id}')
def download(id: int, file_id: int, db: Session = Depends(get_db), user=Depends(view)):
    asset = c.documents(db, id).get(file_id)
    if not asset: c.fail('Документ поставщика не найден', 404)
    return verified_file_response(asset)


@router.post('/suppliers/{id}/documents', status_code=201)
async def upload(id: int, file: UploadFile, db: Session = Depends(get_db), user=Depends(edit)):
    c.supplier(db, id)
    raw = await file.read(20 * 1024 * 1024 + 1)
    if not raw or len(raw) > 20 * 1024 * 1024:
        c.fail('Загрузите PDF или скан до 20 МБ', 422)
    try:
        if raw.startswith(b'%PDF-'):
            from pypdf import PdfReader
            pdf = PdfReader(BytesIO(raw))
            if pdf.is_encrypted or not pdf.pages: raise ValueError()
            content_type = 'application/pdf'
        else:
            from PIL import Image
            with Image.open(BytesIO(raw), formats=('PNG', 'JPEG', 'WEBP')) as image:
                if image.format not in ('PNG', 'JPEG', 'WEBP') or image.width * image.height > 40_000_000: raise ValueError()
                content_type = Image.MIME[image.format]; image.verify()
    except Exception:
        c.fail('Файл не распознан как PDF или скан PNG/JPEG/WEBP', 422)
    filename = (file.filename or 'Документ закупки').replace('\\', '/').split('/')[-1][:255]
    asset = save_bytes_file(db, filename, content_type, raw, FilePurpose.COSTING_EVIDENCE, user)
    db.add(Document(supplier_id=id, file_id=asset.id, actor_id=user.id)); db.commit()
    return {'file_id': asset.id, 'filename': asset.filename, 'sha256': asset.sha256}
