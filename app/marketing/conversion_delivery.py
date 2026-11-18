"""Durable, deduplicated handoff to the existing authorised Metrika session.

An upload intent is recorded BEFORE browser transmission. Ambiguous uploads are
never retried automatically. Processing is distinct from visit attribution.
No token, telephone, email, arbitrary destination, or invented qualification.
"""
import csv
import hashlib
import io
import re
from collections import Counter
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import text

from app.clients.models import ClientLeadRequest
from app.marketing.conversion_models import (
    ConversionDeliveryBatch as Batch, ConversionDeliveryControl as Control,
    ConversionDeliveryEvent as Event, ConversionDeliveryItem as Item,
)

COUNTER = '95674381'
TARGET = 'durov_os_qualified'
TERMINAL = {'processed', 'rejected'}
TRANSITIONS = {
    'prepared': {'uploading', 'rejected'},
    'uploading': {'submitted', 'uncertain', 'rejected'},
    'uncertain': {'submitted', 'rejected'},
    'submitted': {'processed', 'rejected', 'uncertain'},
    'processed': {'processed'},
    'rejected': set(),
}


def csv_content(rows):
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=['ClientId', 'Yclid', 'Target', 'DateTime'])
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue()


def lock(db):
    if db.bind.dialect.name == 'postgresql':
        db.execute(text('SELECT pg_advisory_xact_lock(95674381, 679827862)'))


def event(db, action, actor, batch=None, **details):
    db.add(Event(batch_id=batch.id if batch else None, actor=actor[:100], action=action, details=details))


def configure(db, enabled, actor):
    lock(db)
    row = db.get(Control, 1)
    if row is None:
        row = Control(id=1, enabled=False, scheduler='durov-house-2')
        db.add(row)
    row.enabled = enabled
    event(db, 'scheduler_enabled' if enabled else 'scheduler_disabled', actor)
    db.flush()
    return summary(db, include_batches=True)


def metadata(batch):
    result = {name: getattr(batch, name) for name in (
        'id', 'status', 'version', 'counter_id', 'target', 'row_count', 'payload_sha256',
        'platform_upload_id', 'matched_rows', 'last_note', 'created_at', 'updated_at')}
    for key in ('created_at', 'updated_at'):
        value = result[key]
        result[key] = (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat() if value else None
    return result


def summary(db, *, include_batches=False):
    control = db.get(Control, 1)
    batches = db.query(Batch).order_by(Batch.id.desc()).limit(20).all()
    all_batches = db.query(Batch).all()
    counts = Counter()
    for batch in all_batches:
        counts[batch.status] += batch.row_count
    enabled = bool(control and control.enabled)
    return {
        'mode': 'scheduled_browser' if enabled else 'manual_csv',
        'automatic_enabled': enabled,
        'scheduler': 'durov-house-2' if enabled else None,
        'schedule_moscow': 'Ежедневно в 11:00',
        'last_checked_at': control.last_checked_at.isoformat() if control and control.last_checked_at else None,
        'counter_id': COUNTER, 'target': TARGET,
        'queued_by_status': dict(counts),
        'sent_confirmed': counts['processed'],
        'matched_rows_confirmed': (None if any(b.status == 'processed' and b.matched_rows is None for b in all_batches)
                                   else sum(b.matched_rows or 0 for b in all_batches if b.status == 'processed')),
        'attribution_check_pending': any(b.status == 'processed' and b.matched_rows is None for b in all_batches),
        'batches': [metadata(batch) for batch in batches] if include_batches else [],
        'note': ('Подтверждённые обращения передаются через действующий ежедневный монитор. '
                 'Принятие файла, обработка и привязка к визитам учитываются отдельно.' if enabled else
                 'Передача очереди выключена. Подготовка файла не означает отправку.'),
    }


def held_clients(db):
    batches = {b.id: b for b in db.query(Batch).all()}
    return {item.client_id: (item.request_id, batches[item.batch_id].status)
            for item in db.query(Item).all()}


def prepare(db, actor='scheduled-monitor', *, scheduled=False, now=None):
    from app.marketing.conversions import export_candidates
    lock(db)
    now = now or datetime.now(timezone.utc)
    control = db.get(Control, 1)
    if scheduled and not (control and control.enabled):
        raise HTTPException(409, 'Плановая передача выключена')
    if control:
        control.last_checked_at = now
    # Reconcile any unresolved handoff before creating another file.
    active = db.query(Batch).filter(~Batch.status.in_(TERMINAL)).order_by(Batch.id).first()
    if active:
        return {**metadata(active), 'existing': True}
    canonical, _, _ = export_candidates(db, now=now, with_requests=True)
    if not canonical:
        return {'id': None, 'row_count': 0, 'status': 'no_eligible_requests'}
    content = csv_content([row for _, row in canonical])
    batch = Batch(status='prepared', version=1, counter_id=COUNTER, target=TARGET,
                  row_count=len(canonical), payload_sha256=hashlib.sha256(content.encode()).hexdigest())
    db.add(batch)
    db.flush()
    for request, row in canonical:
        db.add(Item(batch_id=batch.id, request_id=request.id, client_id=request.client_id,
                    target=TARGET, payload=row))
    event(db, 'prepared', actor, batch, rows=len(canonical), sha256=batch.payload_sha256)
    db.flush()
    return {**metadata(batch), 'existing': False}


def file(db, batch_id):
    batch = db.get(Batch, batch_id)
    if not batch:
        raise HTTPException(404, 'Пакет не найден')
    if batch.status != 'prepared':
        raise HTTPException(409, 'Повторная загрузка запрещена; сначала сверьте существующую загрузку в Метрике')
    from app.marketing.conversions import latest_reviews, aware
    reviews = latest_reviews(db)
    items = db.query(Item).filter_by(batch_id=batch_id).order_by(Item.id).all()
    for item in items:
        request = db.get(ClientLeadRequest, item.request_id)
        if not request or aware(request.created_at) < datetime.now(timezone.utc) - timedelta(days=21):
            raise HTTPException(409, 'Обращение вышло за срок сопоставления с визитом')
        review = reviews.get(item.request_id)
        if not review or review.outcome != 'qualified' or not all(
                (review.facts or {}).get(k) for k in ('contact_confirmed', 'need_confirmed', 'region_confirmed')):
            raise HTTPException(409, 'Качество обращения изменилось; пакет нельзя отправлять')
    content = csv_content([item.payload for item in items])
    if hashlib.sha256(content.encode()).hexdigest() != batch.payload_sha256:
        raise HTTPException(409, 'Состав пакета изменился')
    return {'id': batch.id, 'filename': f'durov-os-qualified-batch-{batch.id}.csv', 'csv': content,
            'sha256': batch.payload_sha256, 'rows': batch.row_count, 'sent': False}


def acknowledge(db, batch_id, *, expected_version, status, sha256, actor,
                upload_id=None, matched_rows=None, note=''):
    lock(db)
    batch = db.query(Batch).filter_by(id=batch_id).with_for_update().first()
    if not batch:
        raise HTTPException(404, 'Пакет не найден')
    if sha256 != batch.payload_sha256 or expected_version != batch.version:
        raise HTTPException(409, 'Пакет уже изменён или контрольная сумма не совпадает')
    if status not in TRANSITIONS.get(batch.status, set()):
        raise HTTPException(409, 'Недопустимый переход состояния передачи')
    if status == 'uploading':
        file(db, batch_id)  # Recheck qualification before a transmission intent.
    if status in ('submitted', 'processed'):
        upload_id = upload_id or batch.platform_upload_id
        if not upload_id or not re.fullmatch(r'[0-9]{1,80}', upload_id):
            raise HTTPException(422, 'Укажите фактический номер загрузки из Метрики')
        if batch.platform_upload_id and upload_id != batch.platform_upload_id:
            raise HTTPException(409, 'Номер существующей загрузки нельзя заменить')
        batch.platform_upload_id = upload_id
    if matched_rows is not None:
        if status != 'processed' or not 0 <= matched_rows <= batch.row_count:
            raise HTTPException(422, 'Привязанные строки должны подтверждаться отчётом Метрики')
        batch.matched_rows = matched_rows
    batch.status = status
    batch.version += 1
    batch.last_note = note.strip()[:500]
    event(db, status, actor, batch, upload_id=batch.platform_upload_id,
          matched_rows=matched_rows, sha256=sha256)
    db.flush()
    return metadata(batch)
