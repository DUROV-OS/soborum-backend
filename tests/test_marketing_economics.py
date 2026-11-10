from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from app.clients.models import ClientLeadRequest
from app.common.files import FileAsset
from app.common.module_access import Module, AccessLevel
from app.costing.models import CostingEstimate
from app.crm.models import CRMEntity
from app.marketing import economics
from tests.test_actual_costs import f, body, create, decision, closing_project, P

URL = '/api/marketing/economics?date_from=2026-11-01&date_to=2026-11-30'


def linked(f):
    db = f['db']
    r = ClientLeadRequest(client_id=f['project'].client_id, source='Сайт',
        utm={'utm_source': 'yandex', 'utm_campaign': 'test-only'},
        created_at=datetime(2026, 11, 1, tzinfo=timezone.utc))
    db.add(r); db.flush()
    d = CRMEntity(kind='deal', name='Синтетическая сделка', payload={'client_id': r.client_id, 'client_request_id': r.id})
    db.add(d); db.flush(); f['project'].crm_deal_id = d.id; db.commit()
    return r, d


def report(f):
    r = f['api'](f['owner']).get(URL)
    assert r.status_code == 200, r.text
    return r.json()


def advertising(f):
    return create(f, body(f, line_ref='advertising', gross_amount='122', excluded_tax='22',
        allocations=[{'category': 'advertising', 'project_id': f['project'].id, 'month': None,
                      'amount': '100', 'locator': 'Строка рекламы этого проекта'}]))


def test_confirmed_expense_is_not_payment_or_complete_profit(f):
    linked(f); row = advertising(f)
    before = report(f)
    assert before['period_advertising']['recognized_amount'] is None
    assert before['period_advertising']['pending_count'] == 1
    assert before['groups'][0]['projects'][0]['recognized_advertising'] is None
    assert decision(f, row).status_code == 200
    after = report(f); p = after['groups'][0]['projects'][0]
    assert after['period_advertising']['recognized_amount'] == p['recognized_advertising'] == '100.00'
    assert p['actual_profit'] is None and not p['complete']
    assert after['romi_percent'] is after['cost_per_qualified_contact'] is after['platform_spend'] is None
    proof = p['advertising_documents'][0]
    assert proof['sha256'] == row['proof']['sha256'] and proof['record_id'] == row['id']
    assert proof['checked_by'] and proof['locator'] and proof['allocation_basis']
    assert after['source_hash'] != before['source_hash']
    assert report(f)['source_hash'] == after['source_hash']  # clock is not a business change


def test_file_damage_removes_recognized_advertising(f):
    linked(f); row = advertising(f); assert decision(f, row).status_code == 200
    Path(f['db'].get(FileAsset, f['file_id']).path_on_disk).write_bytes(b'changed')
    result = report(f)
    assert result['period_advertising']['recognized_amount'] is None
    assert result['period_advertising']['stale_count'] == 1
    assert result['groups'][0]['projects'][0]['advertising_documents'][0]['state'] == 'stale'


def test_cancelled_cost_is_excluded(f):
    linked(f); row = advertising(f); assert decision(f, row).status_code == 200
    assert decision(f, row, 'void').status_code == 200
    result = report(f)
    assert not result['period_advertising']['documents']
    assert result['groups'][0]['projects'][0]['recognized_advertising'] is None


def test_one_document_split_between_projects_is_not_counted_twice(f):
    linked(f)
    other = CostingEstimate(title='Второй проект', actor_id=f['owner'].id)
    f['db'].add(other); f['db'].commit()
    row = create(f, body(f, line_ref='shared-advertising', gross_amount='100', allocations=[
        {'category': 'advertising', 'project_id': id, 'month': None, 'amount': amount, 'locator': 'Подтверждённая доля проекта'}
        for id, amount in [(f['project'].id, '40'), (other.id, '60')]]))
    assert decision(f, row).status_code == 200
    result = report(f)
    assert result['period_advertising']['recognized_amount'] == '100.00'
    assert len(result['period_advertising']['documents']) == 1
    assert result['groups'][0]['projects'][0]['recognized_advertising'] == '40.00'


@pytest.mark.parametrize('break_link', ['no_deal', 'archived_deal', 'no_receipt', 'wrong_deal_client', 'wrong_project_client'])
def test_source_is_never_guessed_from_client_or_invalid_links(f, break_link):
    r, d = linked(f)
    if break_link == 'no_deal': f['project'].crm_deal_id = None
    if break_link == 'archived_deal': d.archived = True
    if break_link == 'no_receipt': d.payload = {'client_id': r.client_id}
    if break_link == 'wrong_deal_client': d.payload = {**d.payload, 'client_id': None}
    if break_link == 'wrong_project_client': f['project'].client_id = None
    f['db'].commit()
    result = report(f)
    assert result['linked_projects'] == 0
    assert result['unlinked_projects_all_time'][0]['id'] == f['project'].id


def test_receipt_cohort_and_expense_period_are_separate(f):
    r, _ = linked(f); r.created_at = datetime(2026, 10, 10, tzinfo=timezone.utc); f['db'].commit()
    row = advertising(f); assert decision(f, row).status_code == 200
    result = report(f)
    assert result['total_requests'] == result['linked_projects'] == 0
    assert result['period_advertising']['recognized_amount'] == '100.00'
    assert result['unlinked_projects_all_time'] == []


def test_repeat_receipts_do_not_duplicate_project_profit(f):
    r, _ = linked(f)
    other = ClientLeadRequest(client_id=r.client_id, source='Сайт', utm={'utm_source': 'another'},
        created_at=r.created_at, is_repeat=True)
    f['db'].add(other); f['db'].commit()
    result = report(f)
    assert result['total_requests'] == 2 and result['unique_contacts'] == 1
    assert result['linked_projects'] == 1 and len(result['groups']) == 2


def close(f):
    _, request, shipment = closing_project(f)
    url = P + f'/projects/{f["project"].id}'
    response = f['api'](f['accountant']).post(url+'/accounting-close', json=request)
    assert response.status_code == 200, response.text
    checked = response.json()
    assert report(f)['closed_project_profit'] is None
    response = f['api'](f['owner']).post(url+'/director-close', json={
        'request_id': str(uuid4()), 'source_hash': checked['source_hash'],
        'accounting_closure_id': checked['closure']['accounting_id'], 'confirmed': True,
        'reason': 'Проверен документальный итог'})
    assert response.status_code == 200, response.text
    return shipment


def test_profit_only_after_actual_close_and_disappears_when_basis_changes(f):
    linked(f); shipment = close(f)
    result = report(f); p = result['groups'][0]['projects'][0]
    assert p['actual_profit'] == result['closed_project_profit'] == '1634.98'
    assert p['recognized_advertising'] == '0.00'  # documented zero category in the closing fixture
    shipment.status = 'cancelled'; f['db'].commit()
    assert report(f)['closed_project_profit'] is None


def test_same_order_line_in_two_active_estimates_cannot_double_count(f):
    linked(f); close(f); p = f['project']
    f['db'].add(CostingEstimate(title='Другая смета', actor_id=f['owner'].id,
        sales_order_id=p.sales_order_id, sales_order_line=p.sales_order_line))
    f['db'].commit()
    result = report(f)
    assert result['closed_project_profit'] is None
    assert result['groups'][0]['projects'][0]['closure_state'] == 'duplicate_order_line'


@pytest.mark.parametrize('missing', [Module.MARKETING, Module.CLIENTS, Module.ACCOUNTING, Module.COSTING])
def test_cross_module_financial_access_required(f, missing):
    user = f['make_user'](*[m for m in (Module.MARKETING, Module.CLIENTS, Module.ACCOUNTING, Module.COSTING) if m != missing])
    assert f['api'](user).get(URL).status_code == 403


def test_manager_cannot_gain_internal_profit_with_old_grants(f):
    user = f['make_user'](Module.MARKETING, Module.CLIENTS, Module.ACCOUNTING, Module.COSTING)
    user.job_role = 'manager'; f['db'].commit()
    assert f['api'](user).get(URL).status_code == 403


def test_read_only_accountant_grants_are_sufficient(f):
    user = f['make_user'](Module.MARKETING, Module.CLIENTS, Module.ACCOUNTING, Module.COSTING, level=AccessLevel.VIEW)
    assert f['api'](user).get(URL).status_code == 200


def test_moscow_calendar_boundary_and_invalid_range(f):
    r, _ = linked(f)
    r.created_at = datetime(2026, 10, 31, 21, tzinfo=timezone.utc); f['db'].commit()
    result = economics.report(f['db'], date(2026, 11, 1), date(2026, 11, 1), now=datetime(2026, 11, 2, tzinfo=timezone.utc))
    assert result['total_requests'] == 1 and not result['partial_today']
    r.created_at = datetime(2026, 10, 31, 20, 59, 59, tzinfo=timezone.utc); f['db'].commit()
    assert report(f)['total_requests'] == 0
    assert f['api'](f['owner']).get('/api/marketing/economics?date_from=2026-11-02&date_to=2026-11-01').status_code == 422
