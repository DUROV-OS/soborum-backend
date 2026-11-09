from copy import deepcopy
from decimal import Decimal as D
from uuid import uuid4
import pytest
from app.purchasing.budget import projection
from app.purchasing.control_models import PurchaseControlRecord as Record
from app.sales.models import SalesEvent
from tests.test_purchase_controls import ready, review, price_body, P, native
from tests.test_order_journey import allocation
from app.accounting.models import SupplierOrder


def calculate(rows, cost='650', price='1000'):
    return projection({1: {'quantity': D('4'), 'amount': D('200')}}, rows, D(cost), D(price))


def item(q, amount):
    return {'material_id': 1, 'quantity': str(q), 'amount': str(amount)}


def test_reserves_all_unpurchased_materials_and_other_costs():
    result = calculate([item(1, 40)])
    assert result['committed'] == '40.00'
    assert result['remaining_material_reserve'] == '150.00'
    assert result['other_expense_reserve'] == '450.00'
    assert result['projected_cost'] == '640.00'
    assert not result['issues']


def test_two_individually_small_orders_cannot_spend_remaining_norm_budget():
    result = calculate([item(1, 90), item(1, 90)])
    assert result['committed'] == '180.00'  # below the whole BOM, but no room for remaining norm
    assert result['remaining_material_reserve'] == '100.00'
    assert result['available'] == '-80.00'
    assert len(result['issues']) == 2


@pytest.mark.parametrize('amount,blocked', [('200', False), ('200.000001', True)])
def test_exact_35_percent_boundary_without_float_or_display_rounding(amount, blocked):
    assert bool(calculate([item(4, amount)])['issues']) is blocked


def test_excess_quantity_and_unknown_material_block():
    result = calculate([item(5, 1), {'material_id': 2, 'quantity': '1', 'amount': '1'}])
    assert len(result['issues']) == 2


def test_approval_is_stable_and_not_double_counted(native, db, api):
    client, row, *_ = ready(native, db, api)
    data = review(client, row)
    assert data['decision'] == 'eligible', data
    budget = data['budgets'][0]
    assert budget['committed'] == '200.00'
    assert budget['remaining_material_reserve'] == '0.00'
    body = {'request_id': str(uuid4()), 'fingerprint': data['fingerprint']}
    response = client.post(f'{P}/orders/{row.id}/automatic-approval', json=body)
    assert response.status_code == 200, response.text
    after = review(client, row)
    assert after['fingerprint'] == data['fingerprint']
    assert after['budgets'] == data['budgets']
    assert after['decision'] == 'auto_approved'


def test_production_derived_revision_keeps_month_and_units_in_actual_accounting(native, db, api):
    from app.accounting import actual
    from app.costing.models import CostingRevision
    client, row, *_ = ready(native, db, api)
    budget = review(client, row)['budgets'][0]
    revision = db.get(CostingRevision, budget['revision_id'])
    assert 'production_signoff_id' in revision.request_data
    report = actual.overview(db, budget['estimate_id'], with_closures=False)['projects'][0]
    assert report['overhead']['project_units'] == revision.snapshot['input']['quantity']
    assert report['overhead']['plan_id'] == revision.snapshot['input']['overhead_plan_id']
    assert not any('Не выбран' in issue for issue in report['issues'])


@pytest.mark.parametrize('change', ['released', 'archived', 'cheaper'])
def test_committed_budget_cannot_disappear(native, db, api, change):
    client, row, *_ = ready(native, db, api)
    data = review(client, row)
    response = client.post(f'{P}/orders/{row.id}/automatic-approval', json={
        'request_id': str(uuid4()), 'fingerprint': data['fingerprint']})
    assert response.status_code == 200, response.text
    if change == 'released':
        a = db.query(SalesEvent).filter_by(kind='supply_allocated').one()
        db.add(SalesEvent(order_id=a.order_id, actor_id=native[1].id, kind='supply_released', data={
            'allocation_id': a.id, 'request_id': str(uuid4()), 'reason': 'Отмена планирования'}))
    elif change == 'archived':
        row.archived = True
    else:
        row.items = [{**row.items[0], 'unit_price': 1}]
        row.version += 1
    db.commit()
    data = review(client, row)
    assert data['decision'] == 'manual_review'
    assert D(data['budgets'][0]['commitments'][0]['amount']) == 200
    if change in ('archived', 'cheaper'):
        assert data['budgets'][0]['forecast_margin_percent'] is None


def test_other_approved_order_changes_project_budget_and_fingerprint(native, db, api):
    client, row, previous, *_ = ready(native, db, api)
    data = review(client, row)
    saved = deepcopy(data)
    # A historic obligation remains even if the planning allocation was removed.
    saved['order_id'] = previous.id
    saved['lines'][0]['targets'][0]['quantity'] = '1'
    db.add(Record(kind='approval', supplier_id=row.supplier_id, order_id=previous.id,
                  actor_id=native[1].id, request_id=str(uuid4()), content={'review': saved, 'fingerprint': 'old'}))
    db.commit()
    after = review(client, row)
    assert after['fingerprint'] != data['fingerprint']
    assert after['decision'] == 'manual_review'
    assert after['budgets'][0]['committed'] == '250.00'
    assert any('норму' in i for i in after['budgets'][0]['issues'])


def test_two_active_orders_share_one_budget(native, db, api):
    client, row, previous, receipt, proof, oid, accountant = ready(native, db, api)
    a = db.query(SalesEvent).filter_by(kind='supply_allocated').one()
    db.add(SalesEvent(order_id=oid, actor_id=native[1].id, kind='supply_released', data={'allocation_id': a.id}))
    row.items = [{**row.items[0], 'quantity': 2}]; row.version += 1
    second = SupplierOrder(supplier_id=row.supplier_id, number='Вторая закупка', total_cost=120,
                           items=[{**row.items[0], 'unit_price': 60}])
    db.add(second); db.commit()
    for purchase in (row, second):
        assert allocation(client, oid, purchase, '2')[0].status_code == 200
    checked = api(accountant)
    for purchase in (row, second):
        old = review(checked, purchase)['price']['id']
        response = checked.post(f'{P}/orders/{purchase.id}/price-evidence', json=price_body(purchase, proof, expected_id=old))
        assert response.status_code == 200, response.text
    data = review(client, row)
    assert not data['lines'][0]['issues']  # this order itself meets the price/quantity rules
    assert data['budgets'][0]['committed'] == '220.00'
    assert data['budgets'][0]['available'] == '-20.00'
    assert data['decision'] == 'manual_review'


def test_archived_project_shows_a_blocker_instead_of_breaking_review(native, db, api):
    from app.costing.models import CostingEstimate
    client, row, *_ = ready(native, db, api)
    budget = review(client, row)['budgets'][0]
    db.get(CostingEstimate, budget['estimate_id']).archived = True; db.commit()
    data = review(client, row)
    assert data['decision'] == 'manual_review'
    assert data['budgets'][0]['issues']
    assert 'available' not in data['budgets'][0]


def test_reapproval_carries_forward_previous_amount(native, db, api):
    client, row, previous, receipt, proof, oid, accountant = ready(native, db, api)
    for price in (50, 40):
        if price == 40:
            row.items = [{**row.items[0], 'unit_price': price}]; row.version += 1; db.commit()
            # Replace the planning link with the same immutable quantity solely
            # to isolate retention of the financial approval in this test.
            from app.sales.journey_supply import supply_basis
            a = db.query(SalesEvent).filter_by(kind='supply_allocated').one()
            a.data = {**a.data, 'supply_basis': supply_basis(row, 0)}; db.commit()
            checked = api(accountant)
            response = checked.post(f'{P}/orders/{row.id}/price-evidence', json=price_body(row, proof, expected_id=review(checked,row)['price']['id']))
            assert response.status_code == 200, response.text
        data = review(client, row)
        response = client.post(f'{P}/orders/{row.id}/automatic-approval', json={'request_id':str(uuid4()),'fingerprint':data['fingerprint']})
        assert response.status_code == 200, response.text
        assert review(client,row)['decision'] == 'auto_approved'
        assert review(client,row)['budgets'][0]['committed'] == '200.00'
