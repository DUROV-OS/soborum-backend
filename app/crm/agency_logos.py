"""Bounded raster uploads; the only logo store is an agency-owned database row.

There are no public URLs, storage paths, FileAsset IDs, or payload references.
Normal CRM edits and the previous application therefore preserve every logo.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
import re
import warnings

from fastapi import HTTPException, Request
from fastapi.responses import Response
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import update

from app.common.module_access import Module
from app.core.deps import require_view, require_edit, require_full
from app.crm.models import CRMAgencyLogo, CRMEntity, CRMEvent

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_DIMENSION = 4096
MAX_PIXELS = 8_000_000
MAX_STORED_DIMENSION = 1024
FORMATS = ('JPEG', 'PNG', 'WEBP')
CONFLICT = 'Карточка уже изменена. Обновите её и проверьте текущий логотип перед сохранением.'


class LogoVersion(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: int = Field(ge=1, le=2147483647)


@dataclass(frozen=True)
class NormalizedLogo:
    data: bytes
    width: int
    height: int


def agency(db, user, agency_id, *, write=False, remove=False):
    (require_full if remove else require_edit if write else require_view)(Module.CLIENTS)(user)
    query = db.query(CRMEntity).filter_by(id=agency_id).populate_existing()
    row = (query.with_for_update() if write or remove else query).first()
    if row is None or row.kind != 'deal':
        raise HTTPException(404, 'Карточка агентства не найдена')
    pipeline_query = db.query(CRMEntity).filter_by(id=row.payload.get('pipeline_id')).populate_existing()
    pipeline = (pipeline_query.with_for_update(read=True) if write or remove else pipeline_query).first()
    if not pipeline or pipeline.kind != 'pipeline' or pipeline.payload.get('purpose') != 'agency':
        raise HTTPException(404, 'Карточка не относится к воронке агентств')
    if (write or remove) and (row.archived or pipeline.archived):
        raise HTTPException(409, 'Сначала восстановите карточку и воронку из архива')
    return row


def info(logo):
    if logo is None:
        return None
    return {'content_type': 'image/png', 'width': logo.width, 'height': logo.height,
            'byte_size': logo.byte_size, 'sha256': logo.sha256, 'updated_at': logo.updated_at.isoformat()}


def status(db, user, agency_id):
    row = agency(db, user, agency_id)
    return {'agency_id': row.id, 'version': row.version, 'logo': info(db.get(CRMAgencyLogo, row.id))}


def content(db, user, agency_id, if_match=None):
    row = agency(db, user, agency_id)
    logo = db.get(CRMAgencyLogo, row.id)
    if logo is None:
        raise HTTPException(404, 'Логотип ещё не загружен')
    tag = '"' + logo.sha256 + '"'
    if if_match is not None and if_match != tag:
        raise HTTPException(412, 'Логотип изменился. Обновите карточку.')
    data = logo.image_data
    if (not data.startswith(b'\x89PNG\r\n\x1a\n') or not 0 < len(data) <= MAX_FILE_BYTES
            or len(data) != logo.byte_size or sha256(data).hexdigest() != logo.sha256):
        raise HTTPException(409, 'Сохранённый логотип недоступен. Загрузите изображение заново.')
    return Response(data, media_type='image/png', headers={
        'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff',
        'Content-Security-Policy': "default-src 'none'; sandbox", 'ETag': tag,
        'Content-Disposition': f'inline; filename="agency-logo-{row.id}.png"'})


async def read_upload(request: Request):
    """Raw image body + entity version in If-Match; no filename, path or file ID."""
    if request.headers.get('content-type', '').split(';', 1)[0].strip().lower() not in (
            'image/png', 'image/jpeg', 'image/webp', 'application/octet-stream'):
        raise HTTPException(415, 'Выберите файл PNG, JPEG или WebP')
    version = re.fullmatch(r'"([1-9][0-9]{0,9})"', request.headers.get('if-match', ''))
    if version is None or int(version[1]) > 2147483647:
        raise HTTPException(428, 'Обновите карточку: её версия не указана или некорректна')
    length = request.headers.get('content-length')
    if length and length.isdecimal() and int(length) > MAX_FILE_BYTES:
        raise HTTPException(413, 'Размер файла должен быть не больше 5 МБ')
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_FILE_BYTES:
            raise HTTPException(413, 'Размер файла должен быть не больше 5 МБ')
        body.extend(chunk)
    return int(version[1]), bytes(body)


def normalize(data):
    if not data:
        raise HTTPException(422, 'Выбран пустой файл')
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(413, 'Размер файла должен быть не больше 5 МБ')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(data), formats=FORMATS) as source:
                width, height = source.size
                if not 0 < width <= MAX_DIMENSION or not 0 < height <= MAX_DIMENSION or width * height > MAX_PIXELS:
                    raise HTTPException(413, 'Изображение слишком большое: максимум 4096 пикселей по стороне и 8 мегапикселей')
                if getattr(source, 'n_frames', 1) != 1:
                    raise HTTPException(422, 'Выберите неподвижное изображение без анимации')
                source.verify()
            with Image.open(BytesIO(data), formats=FORMATS) as source:
                source.load()
                oriented = ImageOps.exif_transpose(source)
                oriented.thumbnail((MAX_STORED_DIMENSION, MAX_STORED_DIMENSION), Image.Resampling.LANCZOS)
                mode = 'RGBA' if 'A' in oriented.getbands() or 'transparency' in oriented.info else 'RGB'
                # A fresh canvas carries no EXIF, XMP, ICC, text, thumbnails or appended bytes.
                with Image.new(mode, oriented.size) as clean:
                    clean.paste(oriented.convert(mode))
                    output = BytesIO()
                    clean.save(output, format='PNG')
                    result = NormalizedLogo(output.getvalue(), clean.width, clean.height)
                oriented.close()
        if len(result.data) > MAX_FILE_BYTES:
            raise HTTPException(413, 'Уменьшите изображение и выберите файл заново')
        return result
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise HTTPException(422, 'Файл повреждён или не является изображением PNG, JPEG или WebP') from None


def _bump(db, row, version):
    if row.version != version:
        raise HTTPException(409, CONFLICT)
    changed = db.execute(update(CRMEntity).where(CRMEntity.id == row.id, CRMEntity.version == version)
        .values(version=version + 1, updated_at=datetime.now(timezone.utc)).execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        raise HTTPException(409, CONFLICT)


def save(db, user, agency_id, version, image):
    """Only normalized bytes from this upload can be bound; no external file references."""
    try:
        row = agency(db, user, agency_id, write=True)
        _bump(db, row, version)
        logo = db.get(CRMAgencyLogo, row.id)
        action = 'Логотип компании заменён' if logo else 'Логотип компании добавлен'
        if logo is None:
            logo = CRMAgencyLogo(agency_id=row.id)
            db.add(logo)
        logo.image_data, logo.width, logo.height = image.data, image.width, image.height
        logo.byte_size, logo.sha256 = len(image.data), sha256(image.data).hexdigest()
        logo.uploaded_by_id, logo.updated_at = user.id, datetime.now(timezone.utc)
        db.add(CRMEvent(entity_id=row.id, actor_id=user.id, action='agency_logo_updated', details={'text': action}))
        db.commit()
        db.refresh(row)
        from app.crm.service import out
        return {'entry': out(row, user=user), 'logo': info(logo)}
    except Exception:
        db.rollback()
        raise


def remove(db, user, agency_id, version):
    try:
        row = agency(db, user, agency_id, remove=True)
        if row.version != version:
            raise HTTPException(409, CONFLICT)
        logo = db.get(CRMAgencyLogo, row.id)
        if logo is None:
            raise HTTPException(404, 'Логотип ещё не загружен')
        _bump(db, row, version)
        db.delete(logo)
        db.add(CRMEvent(entity_id=row.id, actor_id=user.id, action='agency_logo_removed', details={'text': 'Логотип компании удалён'}))
        db.commit()
        db.refresh(row)
        from app.crm.service import out
        return {'entry': out(row, user=user), 'logo': None}
    except Exception:
        db.rollback()
        raise
