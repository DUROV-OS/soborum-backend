"""Reviewed business projections and immutable-byte checks for legacy amoCRM data.

No heuristics approve note bodies or document contents. Unknown is closed;
only typed call metadata is readable without a per-record administrator review.
"""
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
from typing import Literal
from urllib.parse import quote

from fastapi import HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from app.common.module_access import Module
from app.crm.models import CRMEntity, CRMImportReview
from app.crm.privacy import can_read_private, private_tokens, PRIVATE_LABEL
from app.moysklad.models import SourceRecord
from app.moysklad.service import attachment_path

FILE_TYPES = ('amocrm/files', 'amocrm/file_versions')
NOTE_TYPES = ('amocrm/leads/notes', 'amocrm/contacts/notes', 'amocrm/companies/notes')
MAX_REVIEW_BYTES = 25 * 1024 * 1024


class ReviewInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    attachment_index: int = Field(-1, ge=-1, le=100)
    expected_review_id: int | None = Field(None, ge=1)
    source_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    disposition: Literal['business', 'restricted', 'unknown']
    content_reviewed: bool = False
    content_digest: str | None = Field(None, pattern=r'^[0-9a-f]{64}$')
    text: str = Field('', max_length=20000)
    filename: str = Field('', max_length=255)


def require_admin(user):
    if not can_read_private(user):
        raise HTTPException(403, 'Классификацию выполняет администратор')


def fingerprint(row):
    value = [row.entity_type, row.source_id, row.title, row.search_text, row.archived, row.payload]
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def latest_review(db, row, index=-1):
    return db.query(CRMImportReview).filter_by(source_record_id=row.id, attachment_index=index).order_by(CRMImportReview.id.desc()).first()


def _source(db, source_id, index):
    row = db.query(SourceRecord).filter_by(id=source_id).populate_existing().with_for_update().first()
    if row is None or (row.entity_type not in FILE_TYPES if index >= 0 else row.entity_type not in NOTE_TYPES):
        raise HTTPException(404, 'Запись не найдена')
    return row


def _bytes(path):
    with path.open('rb') as stream:
        content = stream.read(MAX_REVIEW_BYTES + 1)
    if len(content) > MAX_REVIEW_BYTES:
        raise HTTPException(413, 'Для проверки требуется файл не больше 25 МБ')
    return content


def review_status(db, user, source_id, index=-1):
    require_admin(user)
    row = _source(db, source_id, index)
    review = latest_review(db, row, index)
    result = {'source_record_id': row.id, 'attachment_index': index, 'source_digest': fingerprint(row),
              'review_id': review.id if review else None, 'disposition': review.disposition if review else 'unknown',
              'current': bool(review and review.source_digest == fingerprint(row)),
              'public_data': review.public_data if review else {}}
    if index >= 0:
        path, _ = attachment_path(row, index)
        result['content_digest'] = sha256(_bytes(path)).hexdigest()
        result['current'] = bool(result['current'] and (review.disposition != 'business' or review.content_digest == result['content_digest']))
    return result


def _linked_rows(db, source):
    if source.entity_type in NOTE_TYPES:
        kind = {'leads': 'deal', 'contacts': 'contact', 'companies': 'company'}[source.entity_type.split('/')[1]]
        return db.query(CRMEntity).filter_by(kind=kind, source_id=str(source.payload.get('entity_id'))).all()
    keys = {(str(link.get('id')), {'leads': 'deal', 'contacts': 'contact', 'companies': 'company'}.get(link.get('entity_type')))
            for link in source.payload.get('_linked_entities', []) if isinstance(link, dict)}
    return [row for row in db.query(CRMEntity).filter(CRMEntity.source_id.in_([key[0] for key in keys])).all()
            if (row.source_id, row.kind) in keys]


def _safe_text(db, source, text):
    # This rejects known protected values; it never approves arbitrary content.
    tokens = private_tokens(source.payload)
    for row in _linked_rows(db, source):
        tokens.update(private_tokens(row.payload))
    return bool(text.strip()) and not PRIVATE_LABEL.search(text) and not any(token in text.casefold() for token in tokens)


def record_review(db, user, source_id, payload: ReviewInput):
    require_admin(user)
    row = _source(db, source_id, payload.attachment_index)
    previous = latest_review(db, row, payload.attachment_index)
    if payload.expected_review_id != (previous.id if previous else None) or payload.source_digest != fingerprint(row):
        raise HTTPException(409, 'Запись или её классификация изменились. Проверьте заново')
    public, content_digest = {}, None
    if payload.disposition == 'business':
        if not payload.content_reviewed:
            raise HTTPException(422, 'Подтвердите проверку содержимого')
        if payload.attachment_index < 0:
            if not _safe_text(db, row, payload.text) or re.search(r'https?://|/api/|file://', payload.text, re.I):
                raise HTTPException(422, 'Публичная заметка содержит непроверенные данные или ссылки')
            public = {'text': payload.text.strip()}
        else:
            if not _safe_text(db, row, payload.filename) or re.search(r'[/\\\r\n\x00]', payload.filename):
                raise HTTPException(422, 'Укажите проверенное имя файла')
            path, item = attachment_path(row, payload.attachment_index)
            content_digest = sha256(_bytes(path)).hexdigest()
            if payload.content_digest != content_digest:
                raise HTTPException(409, 'Содержимое файла изменилось. Проверьте заново')
            public = {'filename': payload.filename, 'content_type': item.get('content_type', 'application/octet-stream')}
    review = CRMImportReview(source_record_id=row.id, attachment_index=payload.attachment_index,
        disposition=payload.disposition, source_digest=fingerprint(row), content_digest=content_digest,
        public_data=public, actor_id=user.id)
    db.add(review)
    db.commit()
    return {'review_id': review.id, 'disposition': review.disposition}


def _time(payload):
    value = payload.get('created_at')
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= 253402300799:
        value = 0
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def public_history_entry(db, source):
    review = latest_review(db, source)
    if review and (review.disposition != 'business' or review.source_digest != fingerprint(source)):
        return None
    result = {'id': 'amo-' + source.source_id, 'created_at': _time(source.payload), 'source': 'amocrm'}
    if review:
        text = review.public_data.get('text', '')
        if not _safe_text(db, source, text):
            return None
        return {**result, 'text': text, 'details': {'note_type': 'common', 'params': {'text': text}}}
    kind = source.payload.get('note_type')
    if kind not in ('call_in', 'call_out'):
        return None
    params = source.payload.get('params')
    params = params if isinstance(params, dict) else {}
    phone = params.get('phone')
    phone = re.sub(r'[\s().-]', '', phone) if isinstance(phone, str) else ''
    if not re.fullmatch(r'\+?[1-9]\d{10,14}', phone):
        phone = ''
    if phone and not _safe_text(db, source, phone):
        phone = ''
    duration = params.get('duration')
    if isinstance(duration, str) and duration.isdecimal() and len(duration) <= 5:
        duration = int(duration)
    if not isinstance(duration, int) or isinstance(duration, bool) or not 0 <= duration <= 86400:
        duration = 0
    title = 'Входящий звонок' if kind == 'call_in' else 'Исходящий звонок'
    return {**result, 'text': title, 'details': {'note_type': kind, 'params': {'phone': phone, 'duration': duration}}}


def _source_aliases(db, target):
    from app.core.config import settings
    root = (Path(settings.storage_dir) / 'moysklad' / 'files').resolve()
    result = []
    rows = db.query(SourceRecord).filter(SourceRecord.payload['_downloaded_files'].as_string().isnot(None)).all()
    for row in rows:
        for index, item in enumerate(row.payload.get('_downloaded_files') or []):
            if isinstance(item, dict) and (root / str(item.get('local_name', ''))).resolve() == target:
                result.append((row, index))
    return result


def _check_all_file_aliases(db, user, target, digest=None):
    """The most restrictive known provenance wins; never recurse into downloads."""
    if can_read_private(user):
        return
    from app.common.files import FileAsset, FilePurpose, PURPOSE_MODULE
    for asset in db.query(FileAsset).all():
        if Path(asset.path_on_disk).resolve() != target:
            continue
        required = PURPOSE_MODULE.get(asset.purpose)
        if not required or not user.has_access(required):
            raise HTTPException(403, 'Нет доступа к этому файлу')
        if asset.purpose in (FilePurpose.AI_CHAT_ATTACHMENT, FilePurpose.MEETING_AUDIO) and asset.uploaded_by_id != user.id:
            raise HTTPException(403, 'Нет доступа к этому файлу')
    for source, index in _source_aliases(db, target):
        if source.entity_type in FILE_TYPES:
            review = latest_review(db, source, index)
            if (not review or review.disposition != 'business' or review.source_digest != fingerprint(source)
                    or review.content_digest != digest or not _safe_text(db, source, review.public_data.get('filename', ''))):
                raise HTTPException(403, 'Содержимое файла не разрешено для просмотра')
        else:
            from app.moysklad.service import get_record
            get_record(db, user, source.id)


def authorized_attachment(db, user, source, index=0):
    if not user or not user.has_access(Module.CLIENTS):
        raise HTTPException(403, 'Нет доступа к файлам продаж')
    if can_read_private(user):
        path, item = attachment_path(source, index)
        return path, item, None
    review = latest_review(db, source, index)
    if (not review or review.disposition != 'business' or review.source_digest != fingerprint(source)
            or not _safe_text(db, source, review.public_data.get('filename', ''))):
        raise HTTPException(403, 'Содержимое файла не разрешено для просмотра')
    try:
        path, _ = attachment_path(source, index)
        content = _bytes(path)
    except (HTTPException, OSError, ValueError, KeyError):
        raise HTTPException(403, 'Содержимое файла не разрешено для просмотра') from None
    digest = sha256(content).hexdigest()
    if digest != review.content_digest:
        raise HTTPException(403, 'Содержимое файла не разрешено для просмотра')
    _check_all_file_aliases(db, user, path, digest)
    return path, {**review.public_data, 'size': len(content)}, content


def reviewed_response(item, content):
    # Return the checked bytes, never reopen a mutable original after permission.
    return Response(content, media_type='application/octet-stream', headers={
        'Content-Disposition': "attachment; filename*=utf-8''" + quote(item['filename'], safe=''),
        'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff'})


def extract_reviewed_text(source_id, item, content):
    from app.common.files import FilePurpose
    from app.common.file_text import extract_asset_text
    # The parser sees only the verified bytes, not a path that can be replaced.
    with tempfile.TemporaryDirectory(prefix='crm-reviewed-') as directory:
        path = Path(directory) / 'content'
        path.write_bytes(content)
        asset = SimpleNamespace(id=source_id, filename=item['filename'], path_on_disk=str(path),
            content_type=item.get('content_type', ''), purpose=FilePurpose.AI_CHAT_ATTACHMENT)
        return extract_asset_text(asset, max_chars=20000)


def shared_imported_attachment(db, user, asset):
    """A shared FileAsset alias must not bypass the imported file boundary."""
    from app.core.config import settings
    target = Path(asset.path_on_disk).resolve()
    root = (Path(settings.storage_dir) / 'moysklad' / 'files').resolve()
    if not target.is_relative_to(root):
        return None
    matches = _source_aliases(db, target)
    if not matches:
        if can_read_private(user):
            return None
        raise HTTPException(403, 'Происхождение импортированного файла не подтверждено')
    imported = next(((row, index) for row, index in matches if row.entity_type in FILE_TYPES), None)
    if imported:
        return authorized_attachment(db, user, *imported)
    _check_all_file_aliases(db, user, target)
    permitted = []
    for row, index in matches:
        from app.moysklad.service import get_record
        get_record(db, user, row.id)
        path, item = attachment_path(row, index)
        permitted.append((path, item, None))
    # Prefer verified bytes if another ERP record aliases the same reviewed file.
    return next((item for item in permitted if item[2] is not None), permitted[0])
