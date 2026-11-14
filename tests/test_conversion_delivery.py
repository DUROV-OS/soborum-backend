"""No duplicate signals, no fabricated delivery, and no contact/quality bypass."""
import csv
import io
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.clients.models import ClientLeadRequest
from app.common.module_access import Module
from app.marketing import conversion_delivery as delivery, conversions
from app.marketing.conversion_models import ConversionDeliveryItem, ConversionReview
from test_marketing_conversions import receipt, qualify


def test_direction_is_an_explicit_valid_marker_and_review_coverage_is_not_guessed(db, make_user):
    request = receipt(db, tracking={'client_id':'1234567890123456'})
    request.utm = {'utm_source':'yandex', 'utm_campaign':'715531690',
                   'utm_content':'1926994114517949634.5824961672.search.barn'}
    db.flush()
    today = datetime.now(conversions.MOSCOW).date()
    report = conversions.report(db, today, today)
    assert report['direction_summary'][0]['direction'] == 'Барнхаусы'
    assert report['direction_summary'][0]['qualified_unique_contacts'] == 0
    assert report['quality_review_coverage_percent'] == 0
    qualify(db, request, make_user(admin=True))
    report = conversions.report(db, today, today)
    assert report['quality_review_coverage_percent'] == 100
    assert report['direction_summary'][0]['qualified_unique_contacts'] == 1
    for value in ('barn', '192.582.search.barn.extra', '{ad_id}.{gbid}.search.barn', '192.582.search.unknown'):
        assert conversions.direction_label({'utm_content':value}) == 'Не определено'


def ack(db, batch, status, **kwargs):
    return delivery.acknowledge(db, batch['id'], expected_version=batch['version'], status=status,
        sha256=batch['payload_sha256'], actor='isolated-test', **kwargs)


def test_one_client_one_signal_and_resuming_does_not_create_another_file(db, make_user):
    user = make_user(admin=True)
    request = receipt(db, tracking={'client_id':'1234567890123456','yclid':'987654321'})
    qualify(db, request, user)
    again = receipt(db, tracking={'client_id':'1234567890123456'}, event='tilda/two')
    qualify(db, again, user)
    first = delivery.prepare(db, 'isolated-test')
    second = delivery.prepare(db, 'isolated-test')
    assert first['id'] == second['id'] and first['row_count'] == 1 and second['existing']
    assert db.query(ConversionDeliveryItem).count() == 1
    assert conversions.export_rows(db)[0] == []
    content = delivery.file(db, first['id'])
    row = list(csv.DictReader(io.StringIO(content['csv'])))[0]
    assert set(row) == {'ClientId', 'Yclid', 'Target', 'DateTime'}
    assert 'Synthetic' not in content['csv'] and '89001110001' not in content['csv']


def test_download_upload_processing_and_matching_remain_distinct(db, make_user):
    request = receipt(db, tracking={'client_id':'1234567890123456'})
    qualify(db, request, make_user(admin=True))
    batch = delivery.prepare(db, 'isolated-test')
    assert delivery.file(db, batch['id'])['sent'] is False
    batch = ack(db, batch, 'uploading')
    batch = ack(db, batch, 'submitted', upload_id='1234')
    assert delivery.summary(db)['sent_confirmed'] == 0
    batch = ack(db, batch, 'processed', upload_id='1234')
    summary = delivery.summary(db)
    assert summary['sent_confirmed'] == 1 and summary['attribution_check_pending']
    assert delivery.prepare(db)['row_count'] == 0
    batch = ack(db, batch, 'processed', upload_id='1234', matched_rows=0)
    assert not delivery.summary(db)['attribution_check_pending']
    assert delivery.summary(db)['matched_rows_confirmed'] == 0


def test_uncertain_upload_is_held_for_reconciliation_and_never_reexported(db, make_user):
    request = receipt(db, tracking={'yclid':'123456789'})
    qualify(db, request, make_user(admin=True))
    batch = ack(db, delivery.prepare(db), 'uploading')
    batch = ack(db, batch, 'uncertain')
    held = delivery.prepare(db)
    assert held['id'] == batch['id'] and held['status'] == 'uncertain'
    with pytest.raises(HTTPException) as error:
        delivery.file(db, batch['id'])
    assert error.value.status_code == 409
    assert conversions.export_rows(db)[0] == []


def test_revoked_quality_and_expired_receipt_block_a_prepared_file(db, make_user):
    user = make_user(admin=True)
    request = receipt(db, tracking={'yclid':'123456789'})
    qualify(db, request, user)
    batch = delivery.prepare(db)
    qualify(db, request, user, version=1, outcome='unqualified')
    with pytest.raises(HTTPException):
        ack(db, batch, 'uploading')
    qualify(db, request, user, version=2)
    request.created_at = datetime.now(timezone.utc) - timedelta(days=22)
    db.flush()
    with pytest.raises(HTTPException):
        delivery.file(db, batch['id'])


def test_control_sum_versions_and_platform_receipt_are_required(db, make_user):
    request = receipt(db, tracking={'yclid':'123456789'})
    qualify(db, request, make_user(admin=True))
    batch = delivery.prepare(db)
    with pytest.raises(HTTPException):
        delivery.acknowledge(db, batch['id'], expected_version=1, status='uploading',
            sha256='0'*64, actor='isolated-test')
    with pytest.raises(HTTPException):
        ack(db, batch, 'processed', upload_id='123')
    batch = ack(db, batch, 'uploading')
    with pytest.raises(HTTPException):
        ack(db, batch, 'submitted')
    batch = ack(db, batch, 'submitted', upload_id='123')
    with pytest.raises(HTTPException):
        ack(db, batch, 'processed', upload_id='124')
    with pytest.raises(HTTPException):
        ack(db, batch, 'processed', upload_id='123', matched_rows=2)


def test_shared_visit_keeps_each_contacts_original_qualification_time(db, make_user):
    user = make_user(admin=True)
    first = receipt(db, tracking={'client_id':'1234567890123456'}, event='tilda/one')
    qualify(db, first, user)
    second = receipt(db, tracking={'client_id':'1234567890123456'}, phone='89001110002', event='tilda/two')
    qualify(db, second, user)
    reviews = db.query(ConversionReview).order_by(ConversionReview.id).all()
    reviews[0].created_at = datetime.now(timezone.utc) - timedelta(minutes=20)
    reviews[1].created_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    db.flush()
    batch = delivery.prepare(db)
    rows = list(csv.DictReader(io.StringIO(delivery.file(db, batch['id'])['csv'])))
    assert len(rows) == 2 and rows[0]['DateTime'] != rows[1]['DateTime']


def test_unreviewed_and_missing_identifiers_never_create_a_signal(db, make_user):
    receipt(db, tracking={'client_id':'1234567890123456'})
    qualified_without_id = receipt(db, phone='89001110002', event='tilda/two')
    qualify(db, qualified_without_id, make_user(admin=True))
    assert delivery.prepare(db)['row_count'] == 0
    assert db.query(ConversionDeliveryItem).count() == 0


def test_only_admin_manages_delivery_and_summary_contains_no_export_payload(db, make_user, api):
    request = receipt(db, tracking={'client_id':'1234567890123456'})
    qualify(db, request, make_user(admin=True))
    batch = delivery.prepare(db)
    reader = api(make_user(Module.MARKETING, Module.CLIENTS))
    assert reader.post('/api/marketing/conversion-delivery/control', json={'enabled':True}).status_code == 403
    assert reader.post('/api/marketing/conversion-delivery/batches').status_code == 403
    assert reader.get(f"/api/marketing/conversion-delivery/batches/{batch['id']}/file").status_code == 403
    assert '1234567890123456' not in json.dumps(delivery.summary(db))
    assert delivery.summary(db)['batches'] == []
    control = delivery.configure(db, True, 'isolated-test')
    assert control['automatic_enabled'] and control['scheduler'] == 'durov-house-2'
