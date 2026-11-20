"""Protected attachments scoped to one receipt or supplier invoice."""

import hashlib
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.collaboration.calendar_files import MAX_FILE, validate_file
from app.common.files import FileAsset, FilePurpose, save_bytes_file, verified_file_response
from app.common.module_access import Module
from app.core.deps import require_edit, require_view
from app.db.session import get_db
from app.moysklad.service import attachment_path
from app.purchasing.card_details import invoice_record, source_receipt
from app.purchasing.models import PurchaseDocumentFile
from app.purchasing.order_files import PRIVATE_DOWNLOAD
from app.purchasing.returns import receipt
from app.warehouse.documents import advisory


router = APIRouter()
view = require_view(Module.ACCOUNTING)
edit = require_edit(Module.ACCOUNTING)
Resource = Literal['receipts', 'supplier-invoices']


def scope(resource, id):
    return {'receipt_id': id} if resource == 'receipts' else {'invoice_id': id}


def original(db, resource, id):
    return source_receipt(db, receipt(db, id)) if resource == 'receipts' else invoice_record(db, id)


def assets(db, resource, id):
    return db.query(FileAsset).join(
        PurchaseDocumentFile, PurchaseDocumentFile.asset_id == FileAsset.id,
    ).filter_by(**scope(resource, id)).filter(FileAsset.purpose == FilePurpose.COSTING_EVIDENCE)


def file_row(resource, id, asset):
    return {'key': f'local-{asset.id}', 'filename': asset.filename, 'size': asset.size_bytes,
            'created_at': asset.created_at, 'source': 'Durov OS',
            'path': f'/{resource}/{id}/files/{asset.id}'}


@router.get('/{resource}/{id}/files')
def files(resource: Resource, id: int, db: Session = Depends(get_db), user=Depends(view)):
    source = original(db, resource, id)
    result = [file_row(resource, id, asset) for asset in assets(db, resource, id).order_by(FileAsset.id.desc())]
    if source:
        result.extend({
            'key': f'source-{index}', 'filename': item.get('filename') or f'Файл {index + 1}',
            'size': item.get('size_bytes', item.get('size')), 'source': 'МойСклад', 'created_at': None,
            'path': f'/{resource}/{id}/files/imported/{index}',
        } for index, item in enumerate(source.payload.get('_downloaded_files') or []))
    return result


@router.get('/{resource}/{id}/files/imported/{index}')
def imported_file(resource: Resource, id: int, index: int, db: Session = Depends(get_db), user=Depends(view)):
    source = original(db, resource, id)
    if not source:
        raise HTTPException(404, 'Файл не найден')
    path, item = attachment_path(source, index)
    return FileResponse(path, filename=item.get('filename') or 'Вложение',
                        media_type=item.get('content_type') or 'application/octet-stream', headers=PRIVATE_DOWNLOAD)


@router.get('/{resource}/{id}/files/{file_id}')
def download(resource: Resource, id: int, file_id: int, db: Session = Depends(get_db), user=Depends(view)):
    original(db, resource, id)
    asset = assets(db, resource, id).filter(FileAsset.id == file_id).first()
    if not asset:
        raise HTTPException(404, 'Файл не найден')
    response = verified_file_response(asset)
    for name, value in PRIVATE_DOWNLOAD.items():
        response.headers[name] = value
    return response


@router.post('/{resource}/{id}/files', status_code=201)
def upload(resource: Resource, id: int, file: UploadFile, request_id: UUID = Form(...),
           db: Session = Depends(get_db), user=Depends(edit)):
    if resource == 'supplier-invoices' and invoice_record(db, id).archived:
        raise HTTPException(409, 'Счёт находится в архиве')
    if resource == 'receipts':
        receipt(db, id)
    filename = (file.filename or '').replace('\\', '/').rsplit('/', 1)[-1].strip()
    if not filename or len(filename) > 255 or any(ord(char) < 32 for char in filename):
        raise HTTPException(422, 'Некорректное имя файла')
    data = file.file.read(MAX_FILE + 1)
    if not data or len(data) > MAX_FILE:
        raise HTTPException(413, 'Размер файла должен быть от 1 байта до 50 МБ')
    media = validate_file(filename, data)
    digest = hashlib.sha256(data).hexdigest()
    advisory(db, 'purchase-document-file:' + str(request_id))
    prior = db.query(PurchaseDocumentFile).filter_by(request_id=str(request_id)).first()
    if prior:
        asset = assets(db, resource, id).filter(FileAsset.id == prior.asset_id).first()
        if not asset or asset.uploaded_by_id != user.id or asset.sha256 != digest or asset.filename != filename:
            raise HTTPException(409, 'Ключ загрузки уже использован')
        return file_row(resource, id, asset)
    asset = save_bytes_file(db, filename, media, data, FilePurpose.COSTING_EVIDENCE, user)
    disk_path = Path(asset.path_on_disk)
    try:
        db.add(PurchaseDocumentFile(**scope(resource, id), request_id=str(request_id), asset_id=asset.id))
        db.commit()
    except Exception:
        db.rollback()
        disk_path.unlink(missing_ok=True)
        raise
    return file_row(resource, id, asset)
