"""Attachments linked to one supplier order, with the existing purchase audit trail."""

import hashlib
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.collaboration.calendar_files import MAX_FILE, validate_file
from app.common.files import FileAsset, FilePurpose, save_bytes_file, verified_file_response
from app.common.module_access import Module
from app.core.deps import require_edit, require_view
from app.db.session import get_db
from app.moysklad.models import SourceRecord
from app.moysklad.service import attachment_path
from app.purchasing.control_models import PurchaseControlRecord
from app.purchasing.service import get_order
from app.warehouse.documents import advisory

router = APIRouter()
view = require_view(Module.ACCOUNTING)
edit = require_edit(Module.ACCOUNTING)
KIND = 'order_attachment'
PRIVATE_DOWNLOAD = {'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff',
                    'Content-Security-Policy': 'sandbox'}


def imported_order(db, order):
    if order.source_id:
        return db.query(SourceRecord).filter_by(entity_type='purchaseorder', source_id=order.source_id).first()
    return None


def local_files(db, order_id):
    events = db.query(PurchaseControlRecord).filter_by(order_id=order_id, kind=KIND).order_by(PurchaseControlRecord.id.desc()).all()
    proofs = {event.content['file_id']: event.content['sha256'] for event in events}
    if not proofs:
        return {}
    return {asset.id: asset for asset in db.query(FileAsset).filter(
        FileAsset.id.in_(proofs), FileAsset.purpose == FilePurpose.COSTING_EVIDENCE,
    ).order_by(FileAsset.id.desc()) if asset.sha256 == proofs[asset.id]}


def file_row(order_id, asset):
    return {'key': f'local-{asset.id}', 'filename': asset.filename, 'size': asset.size_bytes,
            'created_at': asset.created_at, 'source': 'Durov OS', 'path': f'/orders/{order_id}/files/{asset.id}'}


@router.get('/orders/{order_id}/files')
def files(order_id: int, db: Session = Depends(get_db), user=Depends(view)):
    order = get_order(db, order_id)
    result = [file_row(order_id, asset) for asset in local_files(db, order_id).values()]
    original = imported_order(db, order)
    if original:
        for index, item in enumerate(original.payload.get('_downloaded_files') or []):
            result.append({'key': f'source-{index}', 'filename': item.get('filename') or f'Файл {index + 1}',
                           'size': item.get('size_bytes', item.get('size')), 'source': 'МойСклад', 'created_at': None,
                           'path': f'/orders/{order_id}/files/imported/{index}'})
    return result


@router.get('/orders/{order_id}/files/imported/{index}')
def imported_file(order_id: int, index: int, db: Session = Depends(get_db), user=Depends(view)):
    original = imported_order(db, get_order(db, order_id))
    if not original:
        raise HTTPException(404, 'Файл не найден')
    path, item = attachment_path(original, index)
    return FileResponse(path, filename=item.get('filename') or 'Файл заказа',
                        media_type=item.get('content_type') or 'application/octet-stream', headers=PRIVATE_DOWNLOAD)


@router.get('/orders/{order_id}/files/{file_id}')
def download(order_id: int, file_id: int, db: Session = Depends(get_db), user=Depends(view)):
    get_order(db, order_id)
    asset = local_files(db, order_id).get(file_id)
    if not asset:
        raise HTTPException(404, 'Файл не найден')
    response = verified_file_response(asset)
    for name, value in PRIVATE_DOWNLOAD.items():
        response.headers[name] = value
    return response


@router.post('/orders/{order_id}/files', status_code=201)
def upload(order_id: int, file: UploadFile, request_id: UUID = Form(...),
           db: Session = Depends(get_db), user=Depends(edit)):
    filename = (file.filename or '').replace('\\', '/').rsplit('/', 1)[-1].strip()
    if not filename or len(filename) > 255 or any(ord(char) < 32 for char in filename):
        raise HTTPException(422, 'Некорректное имя файла')
    data = file.file.read(MAX_FILE + 1)
    if not data or len(data) > MAX_FILE:
        raise HTTPException(413, 'Размер файла должен быть от 1 байта до 50 МБ')
    media = validate_file(filename, data)
    digest = hashlib.sha256(data).hexdigest()
    advisory(db, 'purchase-file:' + str(request_id))
    order = get_order(db, order_id, lock=True)
    prior = db.query(PurchaseControlRecord).filter_by(request_id=str(request_id)).first()
    if prior:
        if (prior.kind != KIND or prior.order_id != order_id or prior.actor_id != user.id
                or prior.content['sha256'] != digest or prior.content['filename'] != filename):
            raise HTTPException(409, 'Ключ загрузки уже использован')
        asset = local_files(db, order_id).get(prior.content['file_id'])
        if asset is None:
            raise HTTPException(409, 'Ранее загруженный файл недоступен')
        return file_row(order_id, asset)
    if order.archived:
        raise HTTPException(409, 'Заказ находится в архиве')
    asset = save_bytes_file(db, filename, media, data, FilePurpose.COSTING_EVIDENCE, user)
    disk_path = Path(asset.path_on_disk)
    try:
        db.add(PurchaseControlRecord(order_id=order_id, supplier_id=order.supplier_id, kind=KIND,
                                     actor_id=user.id, request_id=str(request_id),
                                     content={'file_id': asset.id, 'sha256': digest, 'filename': filename}))
        db.commit()
    except Exception:
        db.rollback()
        disk_path.unlink(missing_ok=True)
        raise
    return file_row(order_id, asset)
