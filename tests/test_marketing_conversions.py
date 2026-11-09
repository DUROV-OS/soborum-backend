"""Receipts, feedback and privacy verified against an isolated database."""
from datetime import datetime, timedelta, timezone
import csv
import io
import pytest
from fastapi.testclient import TestClient
from app.clients import intake_auth
from app.clients.models import Client, ClientLeadRequest
from app.clients.service import intake_lead
from app.clients.schemas import LeadIntakePayload
from app.core.config import settings
from app.db.session import get_db
from app.main import app
from app.marketing import conversions
from app.marketing.conversion_models import ConversionReview
from app.marketing.conversion_router import QualityReview


def receipt(db, *, tracking=None, phone='89001110001', event='tilda/one', days=0):
    intake_lead(db, LeadIntakePayload(name='Synthetic person', phone=phone,
        external_event_id=event, tracking=tracking, utm={'utm_source':'yandex','utm_campaign':'715531690'}))
    r = db.query(ClientLeadRequest).order_by(ClientLeadRequest.id.desc()).first()
    r.created_at = datetime.now(timezone.utc)-timedelta(days=days)
    db.flush()
    return r


def qualify(db, r, user, version=0, outcome='qualified'):
    p = QualityReview(expected_version=version, outcome=outcome, reason='Подтверждены потребность и регион',
                      contact_confirmed=True, need_confirmed=True, region_confirmed=True)
    return conversions.review_request(db, r.id, p, user)


def test_tilda_retry_is_one_receipt_and_does_not_reopen_processed_lead(db, monkeypatch):
    monkeypatch.setattr(settings, 'lead_intake_token', 'test-key')
    intake_auth.reset_rate_limit()
    apps = [r.app for r in app.routes if hasattr(r, 'app') and hasattr(r.app, 'dependency_overrides')]
    for sub in apps: sub.dependency_overrides[get_db] = lambda: db
    try:
        c = TestClient(app)
        data = {'Phone':'89001110001', 'Name':'Synthetic', 'tranid':'8027489:12345',
                'yclid':'123456789', 'ym_client_id':'987654321', 'formid':'form123',
                'form_source':'quiz', 'page_url':'https://durov.house/quiz?email=private@example.com',
                'cookies':'do-not-store', 'utm_source':'yandex'}
        assert c.post('/api/clients/intake', data=data, headers={'X-Intake-Token':'test-key'}).status_code == 200
        client = db.query(Client).one()
        client.lead_unprocessed = False
        db.commit()
        assert c.post('/api/clients/intake', data=data, headers={'X-Intake-Token':'test-key'}).status_code == 200
        assert db.query(Client).count() == 1
        r = db.query(ClientLeadRequest).one()
        assert r.external_event_id == 'tilda/8027489:12345'
        assert r.tracking == {'yclid':'123456789','client_id':'987654321','form_id':'form123','form_name':'quiz','page_url':'https://durov.house/quiz'}
        assert r.message == ''
        assert not client.lead_unprocessed
        data['tranid'] = '8027489:67890'
        c.post('/api/clients/intake', data=data, headers={'X-Intake-Token':'test-key'})
        assert db.query(ClientLeadRequest).count() == 2
        assert db.query(Client).count() == 1
        assert client.lead_unprocessed
    finally:
        for sub in apps: sub.dependency_overrides.clear()
        intake_auth.reset_rate_limit()


def test_quality_needs_facts_and_does_not_follow_stage_automatically(db, make_user):
    r = receipt(db, tracking={'yclid':'1234'})
    assert conversions.export_rows(db)[0] == []
    with pytest.raises(ValueError):
        QualityReview(expected_version=0, outcome='qualified', reason='Просто выбрана стадия')
    user = make_user(admin=True)
    qualify(db, r, user)
    assert len(conversions.export_rows(db)[0]) == 1
    with pytest.raises(Exception) as e:
        qualify(db, r, user)
    assert e.value.status_code == 409
    qualify(db, r, user, version=1, outcome='spam')
    assert conversions.export_rows(db)[0] == []
    assert db.query(ConversionReview).count() == 2


def test_export_omits_missing_ids_old_receipts_and_repeat_contacts(db, make_user):
    user = make_user(admin=True)
    a = receipt(db, tracking={'client_id':'111', 'yclid':'222'})
    qualify(db, a, user)
    b = receipt(db, tracking={'yclid':'333'}, event='tilda/two')
    qualify(db, b, user)
    c = receipt(db, phone='89001110002', event='tilda/three')
    qualify(db, c, user)
    d = receipt(db, tracking={'yclid':'444'}, phone='89001110003', event='tilda/four', days=22)
    qualify(db, d, user)
    exported = conversions.export_csv(db)
    assert exported['rows'] == 1 and exported['sent'] is False
    assert exported['excluded'] == {'repeat_qualified_contact':1,'missing_visit_id':1,'outside_matching_window':1}
    today = datetime.now(conversions.MOSCOW).date()
    result = conversions.report(db, today-timedelta(days=30), today)
    statuses = {row['id']:row['feedback_status'] for row in result['rows']}
    assert statuses == {a.id:'ready_for_export', b.id:'repeat_qualified_contact', c.id:'missing_visit_id', d.id:'outside_matching_window'}
    assert list(csv.DictReader(io.StringIO(exported['csv'])))[0]['Target'] == 'durov_os_qualified'
    assert 'Synthetic' not in exported['csv'] and '8900' not in exported['csv']
    old_time = list(csv.DictReader(io.StringIO(exported['csv'])))[0]['DateTime']
    qualify(db, a, user, version=1)
    assert list(csv.DictReader(io.StringIO(conversions.export_csv(db)['csv'])))[0]['DateTime'] == old_time


def test_aggregate_has_no_personal_data_and_permissions_are_enforced(db, make_user, api):
    receipt(db, tracking={'yclid':'123456'})
    today = datetime.now(timezone.utc).astimezone(conversions.MOSCOW).date()
    marketer = make_user('marketing')
    c = api(marketer)
    report = c.get('/api/marketing/conversions', params={'date_from':str(today),'date_to':str(today)}).json()
    assert report['total_requests'] == 1 and report['with_visit_id'] == 1
    assert report['rows'][0]['client_id'] is None
    assert report['can_review'] is False
    assert 'Synthetic' not in str(report) and '123456' not in str(report)
    assert c.get('/api/marketing/conversions-export').status_code == 403
    assert c.post('/api/marketing/conversions/1/review',json={}).status_code == 403
    warehouse = make_user('warehouse')
    assert api(warehouse).get('/api/marketing/conversions').status_code == 403
    assert api(make_user(admin=True)).get('/api/marketing/conversions',params={'date_from':'2020-01-01','date_to':'2026-11-09'}).status_code == 422


def test_tracking_allowlist_rejects_injected_identifiers_and_private_urls():
    assert conversions.tracking_fields({'yclid':'=HYPERLINK(1)','ym_client_id':'x','cookies':'secret',
        'page_url':'https://other.test/private?token=secret'}) is None
    assert conversions.event_key({'tranid':'\nmalicious'}) == 'tilda/malicious'
    assert conversions.event_key({'tranid':'a/b'}) is None


def test_report_period_is_moscow_and_keeps_unknown_attribution(db):
    r = receipt(db)
    r.utm = None
    r.created_at = datetime(2026,11,8,21,30,tzinfo=timezone.utc)
    db.flush()
    from datetime import date
    result = conversions.report(db,date(2026,11,9),date(2026,11,9))
    assert result['total_requests'] == 1 and result['with_visit_id'] == 0
    assert result['rows'][0]['source'] == 'Не определён'
    assert result['delivery']['automatic_enabled'] is False


def test_marketing_ai_gets_safe_acquisition_facts_and_cannot_turn_unknown_roi_green(db):
    from app.dashboard.service import _snapshot_marketing
    from app.ai.analytics import status_floor
    receipt(db, tracking={'yclid':'123456'})
    snapshot = _snapshot_marketing(db)
    assert snapshot['acquisition']['total_requests'] == 1
    assert snapshot['acquisition']['cost_per_qualified_contact'] is None
    assert 'Synthetic' not in str(snapshot) and '123456' not in str(snapshot)
    floor, reason, issues = status_floor('marketing',snapshot)
    assert floor == 'yellow' and issues
