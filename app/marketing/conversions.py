"""Website receipts and independently recorded sales quality; no invented attribution."""
import csv
import io
import re
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo
from fastapi import HTTPException
from sqlalchemy import func
from app.clients.models import ClientLeadRequest
from app.crm.models import CRMEntity
from app.marketing.conversion_models import ConversionReview

MOSCOW = ZoneInfo('Europe/Moscow')
TARGET = 'durov_os_qualified'
OUTCOMES = {'unreviewed', 'qualified', 'unqualified', 'spam', 'duplicate'}
UTM_KEYS = ('utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term')


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def tracking_fields(fields):
    """Allowlist only. Never read COOKIES or store arbitrary URL query strings."""
    result = {}
    aliases = {'yclid': ('yclid',), 'client_id': ('ym_client_id', 'metrika_client_id'),
               'form_id': ('formid',), 'form_name': ('formname', 'form_source')}
    for key, names in aliases.items():
        value = next((fields.get(name) for name in names if isinstance(fields.get(name), str) and fields[name].strip()), '')
        value = value.strip()[:255]
        if key in ('yclid', 'client_id') and not re.fullmatch(r'[0-9]{1,64}', value):
            continue
        if value:
            result[key] = value
    page = fields.get('page_url') or fields.get('referer')
    if isinstance(page, str):
        try:
            u = urlsplit(page)
            if u.scheme == 'https' and u.hostname in ('durov.house', 'www.durov.house') and not u.username:
                result['page_url'] = urlunsplit((u.scheme, u.hostname, u.path[:1500], '', ''))
        except ValueError:
            pass
    return result or None


def event_key(fields):
    value = fields.get('tranid')
    if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9:_-]{1,200}', value.strip()):
        return 'tilda/' + value.strip()
    return None


def latest_reviews(db):
    ids = db.query(func.max(ConversionReview.id)).group_by(ConversionReview.request_id)
    return {r.request_id: r for r in db.query(ConversionReview).filter(ConversionReview.id.in_(ids)).all()}


def date_window(start: date, end: date):
    if end < start or (end - start).days > 366:
        raise HTTPException(422, 'Выберите период не больше 367 дней')
    return (datetime.combine(start, time.min, MOSCOW).astimezone(timezone.utc),
            datetime.combine(end + timedelta(days=1), time.min, MOSCOW).astimezone(timezone.utc))


def report(db, start, end, *, can_review=False, can_open_clients=False, now=None):
    now = now or datetime.now(timezone.utc)
    begin, finish = date_window(start, end)
    reviews = latest_reviews(db)
    requests = db.query(ClientLeadRequest).filter(ClientLeadRequest.created_at >= begin,
                ClientLeadRequest.created_at < finish).order_by(ClientLeadRequest.created_at.desc(), ClientLeadRequest.id.desc()).all()
    request_ids = {r.id for r in requests}
    entities = db.query(CRMEntity).filter(CRMEntity.kind.in_(('inquiry', 'deal'))).all()
    linked = {}
    for entity in entities:
        rid = (entity.payload or {}).get('client_request_id')
        if rid in request_ids:
            linked.setdefault(rid, []).append(entity)
    rows, issues = [], []
    for r in requests:
        review = reviews.get(r.id)
        outcome = review.outcome if review else 'unreviewed'
        tracking = r.tracking or {}
        utm = r.utm or {}
        identifiers = bool(tracking.get('client_id') or tracking.get('yclid'))
        inquiry_open = any(e.kind == 'inquiry' and not e.archived for e in linked.get(r.id, []))
        rows.append({'id': r.id, 'received_at': aware(r.created_at).isoformat(),
            'client_id': r.client_id if can_open_clients else None,
            'repeat_contact': r.is_repeat, 'source': str(utm.get('utm_source') or 'Не определён'),
            'medium': str(utm.get('utm_medium') or ''), 'campaign': str(utm.get('utm_campaign') or ''),
            'form': tracking.get('form_name') or tracking.get('form_id') or '',
            'outcome': outcome, 'version': review.version if review else 0,
            'reason': review.reason if review and can_open_clients else '',
            'has_visit_id': identifiers, 'has_utm': any(utm.get(k) for k in UTM_KEYS),
            'receipt_deduplicated': bool(r.external_event_id), 'sales_inbox_open': inquiry_open,
            'awaiting_quality_hours': round((now-aware(r.created_at)).total_seconds()/3600, 1) if outcome == 'unreviewed' else None,
            'feedback_status': 'awaiting_quality' if outcome == 'unreviewed' else
                ('excluded' if outcome != 'qualified' else ('ready_for_export' if identifiers else 'missing_visit_id'))})
    counts = dict(Counter(r['outcome'] for r in rows))
    missing = sum(not r['has_visit_id'] for r in rows)
    unreviewed = counts.get('unreviewed', 0)
    if missing:
        issues.append(f'Обращения без идентификатора визита: {missing}. Их источник нельзя достоверно связать с рекламой.')
    if unreviewed:
        issues.append(f'Ожидают оценки качества в Durov OS: {unreviewed}.')
    ready, excluded = export_rows(db, now=now)
    return {'date_from': start.isoformat(), 'date_to': end.isoformat(), 'timezone': 'Europe/Moscow',
        'checked_at': now.isoformat(), 'partial_today': end >= now.astimezone(MOSCOW).date(),
        'scope': 'website_receipts', 'total_requests': len(rows), 'unique_contacts': len({r.client_id for r in requests}),
        'outcomes': counts, 'qualified_unique_contacts': len({r.client_id for r in requests if reviews.get(r.id) and reviews[r.id].outcome == 'qualified'}),
        'with_visit_id': len(rows)-missing, 'unprocessed_inbox': sum(r['sales_inbox_open'] for r in rows),
        'can_review': can_review, 'can_open_clients': can_open_clients, 'issues': issues, 'rows': rows,
        'delivery': {'mode': 'manual_csv', 'automatic_enabled': False, 'target': TARGET,
            'exportable_now': len(ready), 'excluded_from_export': excluded, 'sent_confirmed': None,
            'note': 'Файл можно подготовить. Автоматическая передача и привязка к визитам ещё не подтверждены.'}}


def review_request(db, request_id, payload, actor):
    r = db.query(ClientLeadRequest).filter_by(id=request_id).with_for_update().first()
    if r is None:
        raise HTTPException(404, 'Обращение не найдено')
    previous = db.query(ConversionReview).filter_by(request_id=request_id).order_by(ConversionReview.version.desc()).first()
    version = previous.version if previous else 0
    if payload.expected_version != version:
        raise HTTPException(409, 'Оценка уже изменена. Обновите список перед сохранением.')
    row = ConversionReview(request_id=request_id, version=version+1, outcome=payload.outcome,
        reason=payload.reason, facts={'contact_confirmed': payload.contact_confirmed,
        'need_confirmed': payload.need_confirmed, 'region_confirmed': payload.region_confirmed}, actor_id=actor.id)
    db.add(row)
    db.flush()
    return {'request_id': request_id, 'version': row.version, 'outcome': row.outcome}


def export_rows(db, *, now=None):
    """One qualified signal per client. CSV creation is NOT successful delivery.

    Eligibility uses the first confirmed qualification time, even after review
    edits, and the original request date. Old/missing IDs are never fabricated.
    """
    now = now or datetime.now(timezone.utc)
    reviews = latest_reviews(db)
    qualified = [(r, reviews[r.id]) for r in db.query(ClientLeadRequest).order_by(ClientLeadRequest.created_at, ClientLeadRequest.id)
                 if r.id in reviews and reviews[r.id].outcome == 'qualified']
    rows, excluded, seen = [], Counter(), set()
    for request, review in qualified:
        if request.client_id in seen:
            excluded['repeat_qualified_contact'] += 1
            continue
        seen.add(request.client_id)
        tracking = request.tracking or {}
        if not (tracking.get('client_id') or tracking.get('yclid')):
            excluded['missing_visit_id'] += 1
            continue
        first = db.query(ConversionReview).filter_by(request_id=request.id, outcome='qualified').order_by(ConversionReview.id).first()
        occurred = aware(first.created_at)
        if aware(request.created_at) < now - timedelta(days=21) or occurred > now:
            excluded['outside_matching_window'] += 1
            continue
        rows.append({'ClientId': tracking.get('client_id', ''), 'Yclid': tracking.get('yclid', ''),
                     'Target': TARGET, 'DateTime': int(occurred.timestamp())})
    return rows, dict(excluded)


def export_csv(db):
    rows, excluded = export_rows(db)
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=['ClientId', 'Yclid', 'Target', 'DateTime'])
    writer.writeheader()
    writer.writerows(rows)
    return {'filename': 'durov-os-qualified.csv', 'csv': stream.getvalue(), 'rows': len(rows),
            'excluded': excluded, 'sent': False, 'target': TARGET}


def acquisition_summary(db):
    today = datetime.now(MOSCOW).date()
    result = report(db, today.replace(day=1), today)
    return {key: result[key] for key in ('date_from', 'date_to', 'scope', 'partial_today',
        'total_requests', 'unique_contacts', 'qualified_unique_contacts', 'with_visit_id', 'outcomes', 'issues')} | {
        'recorded_ad_spend': None, 'cost_per_qualified_contact': None,
        'offline_delivery_confirmed': False,
        'limitations': 'Только заявки сайта. Не все звонки и обращения. Нет сверенного расхода и подтверждения отправки конверсий.'}
