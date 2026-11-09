from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal as D
from uuid import uuid4

import pytest

from app.accounting.models import SupplierOrder, MoneyMovement
from app.common.module_access import Module, AccessLevel
from app.costing.models import CostingEstimate
from app.moysklad.recipes import ManufactureRun
from app.purchasing.models import PurchaseReceipt
from app.sales.models import SalesEvent, SalesOrder
from app.warehouse.models import Supplier, StockMovement, Warehouse
from tests.test_costing_integration import native, approved, new_order
from tests.test_customer_sales import setup, order as unpriced_order, shipment, post, reserve


def prepared(native, db, norm=False):
    client = native[0]
    estimate = approved(native, db)
    link, _ = new_order(native, estimate)
    oid = link['order_id']
    if norm:
        response = client.post(f'/api/costing/integration/estimates/{estimate["id"]}/production-recipe', json={
            'version': estimate['version'], 'request_id': str(uuid4()), 'reason': 'Проверка маршрута', 'order_id': oid, 'line_index': 0})
        assert response.status_code == 200, response.text
        recipe_id = response.json()['recipe_id']
    else:
        recipe_id = None
    response = client.post(f'/api/sales/orders/{oid}/confirm', json={'version': 1})
    assert response.status_code == 200, response.text
    return oid, estimate, recipe_id


def journey(client, oid):
    response = client.get(f'/api/sales/orders/{oid}/journey')
    assert response.status_code == 200, response.text
    return response.json()


def purchase(db, material, quantity='2'):
    supplier = Supplier(name='Тестовый поставщик маршрута')
    db.add(supplier); db.flush()
    row = SupplierOrder(supplier_id=supplier.id, number='ЗП-тест', total_cost=100, items=[{
        'warehouse_material_id': material.id, 'material': material.title, 'quantity': float(quantity),
        'unit_price': 50, 'unit': material.unit, 'stock_tracked': True, 'received_quantity': 0}])
    db.add(row); db.commit()
    return row


def allocation(client, oid, supply, quantity='1', **changes):
    body = {'request_id': str(uuid4()), 'version': journey(client, oid)['version'], 'order_line': 0,
            'supplier_order_id': supply.id, 'supplier_version': supply.version, 'supplier_line': 0, 'quantity': quantity, **changes}
    return client.post(f'/api/sales/orders/{oid}/supply-allocations', json=body), body


def test_route_tracks_real_norm_partial_run_reserve_shipment_and_cancel(native, db):
    client, _, raw, product, *_ = native
    oid, estimate, rid = prepared(native, db, norm=True)
    first = journey(client, oid)['lines'][0]
    assert first['next']['code'] == 'manufacture'
    assert first['estimate']['approved'] and all(s['signoff_id'] for s in first['estimate']['stages'])
    body = {'warehouse': Warehouse.TECHNOLOGY.value, 'multiplier': '0.5', 'request_id': str(uuid4())}
    response = client.post(f'/api/moysklad/techcards/{rid}/manufacture', json=body)
    assert response.status_code == 200, response.text
    row = journey(client, oid)['lines'][0]
    assert row['recipe']['completed_fraction'] == '0.5'
    assert D(row['materials'][0]['remaining']) == 2 and D(row['free_for_line']) == 1
    assert row['recipe']['runs'][0]['issue_operation_id'] == response.json()['issue_operation_id']
    run2 = client.post(f'/api/moysklad/techcards/{rid}/manufacture', json={**body, 'request_id': str(uuid4())})
    assert run2.status_code == 200
    assert journey(client, oid)['lines'][0]['next']['code'] == 'reserve'
    order = client.get(f'/api/sales/orders/{oid}').json()
    reserve(client, order, product, '2')
    assert journey(client, oid)['lines'][0]['next']['code'] == 'ship'
    doc = shipment(client, order, '2')
    assert D(journey(client, oid)['lines'][0]['shipped']) == 0
    response, _ = post(client, doc)
    assert response.status_code == 200, response.text
    assert journey(client, oid)['lines'][0]['next']['code'] == 'shipped'
    response = client.post(f'/api/sales/shipments/{doc["id"]}/cancel', json={'version': response.json()['version'], 'request_id': str(uuid4()), 'note': 'Проверка отмены'})
    assert response.status_code == 200, response.text
    assert D(journey(client, oid)['lines'][0]['remaining']) == 2


def test_movement_tamper_does_not_count_a_run(native, db):
    client = native[0]
    oid, _, rid = prepared(native, db, norm=True)
    response = client.post(f'/api/moysklad/techcards/{rid}/manufacture', json={'warehouse': Warehouse.TECHNOLOGY.value, 'multiplier': '0.5', 'request_id': str(uuid4())})
    assert response.status_code == 200
    movement = db.query(StockMovement).filter_by(operation_id=response.json()['receipt_operation_id']).one()
    movement.delta = 0; db.commit()  # SQLite fixture deliberately simulates damaged evidence.
    line = journey(client, oid)['lines'][0]
    assert not line['recipe']['valid'] and not line['recipe']['runs'] and line['next']['code'] == 'reconcile'


def test_shared_free_stock_not_counted_twice(db, api, make_user):
    client, _, cp, org, material = setup(db, api, make_user, quantity=3)
    row, _ = unpriced_order(client, cp, org, material, lines=[{'material_id': material.id, 'quantity': '2', 'unit_price': '100'}] * 2)
    data = journey(client, row['id'])
    assert [D(l['free_for_line']) for l in data['lines']] == [D(2), D(1)]
    assert [D(l['shortage']) for l in data['lines']] == [D(0), D(1)]
    assert all(l['next']['code'] == 'approval' for l in data['lines'])


def test_explicit_allocation_limits_retries_release_and_no_stock_or_money(native, db):
    client, _, _, product, *_ = native
    oid, _, _ = prepared(native, db)
    supply = purchase(db, product)
    original = product.quantity_in_stock
    response, body = allocation(client, oid, supply, '2')
    assert response.status_code == 200, response.text
    aid = response.json()['id']
    assert client.post(f'/api/sales/orders/{oid}/supply-allocations', json=body).json()['id'] == aid
    assert allocation(client, oid, supply)[0].status_code == 409
    assert client.post(f'/api/sales/orders/{oid}/supply-allocations', json={**body, 'quantity': '1'}).status_code == 409
    assert db.query(SalesEvent).filter_by(kind='supply_allocated').count() == 1
    assert db.query(StockMovement).count() == db.query(MoneyMovement).count() == 0
    db.refresh(product); assert product.quantity_in_stock == original
    data = journey(client, oid)
    assert data['lines'][0]['next']['code'] == 'receive'
    release = {'request_id': str(uuid4()), 'version': data['version'], 'reason': 'Перераспределение заказа'}
    assert client.post(f'/api/sales/orders/{oid}/supply-allocations/{aid}/release', json=release).status_code == 200
    assert client.post(f'/api/sales/orders/{oid}/supply-allocations/{aid}/release', json=release).status_code == 200
    assert not journey(client, oid)['lines'][0]['purchases']
    assert db.query(SalesEvent).filter_by(kind='supply_allocated').count() == 1
    assert db.query(SalesEvent).filter_by(kind='supply_released').count() == 1
    assert allocation(client, oid, supply, '2')[0].status_code == 200


def test_source_changes_make_allocation_stale_without_freeing_capacity(native, db):
    client, _, _, product, *_ = native
    oid, _, _ = prepared(native, db)
    supply = purchase(db, product)
    assert allocation(client, oid, supply, '2')[0].status_code == 200
    items = deepcopy(supply.items); items[0]['unit_price'] = 51; supply.items = items; supply.version += 1; db.commit()
    data = journey(client, oid)
    assert not data['lines'][0]['purchases'][0]['fresh']
    assert data['lines'][0]['next']['code'] == 'reconcile'
    assert client.get(f'/api/sales/orders/{oid}/supply-options?order_line=0').json() == []
    assert allocation(client, oid, supply)[0].status_code == 409


def test_receipt_not_equal_reserved_stock_and_receipt_changes_keep_basis(native, db):
    client, owner, _, product, *_ = native
    oid, _, _ = prepared(native, db)
    supply = purchase(db, product)
    assert allocation(client, oid, supply, '1')[0].status_code == 200
    items = deepcopy(supply.items); items[0]['received_quantity'] = 1; supply.items = items; supply.version += 1
    db.add(PurchaseReceipt(order_id=supply.id, supplier_id=supply.supplier_id, actor_id=owner.id, number='ПР-тест',
        occurred_at=datetime.now(timezone.utc), status='posted', lines=[{'order_line': 0, 'material_id': product.id, 'quantity': 1}]))
    db.commit()
    row = journey(client, oid)['lines'][0]
    assert row['purchases'][0]['fresh'] and D(row['purchases'][0]['receipts'][0]['quantity']) == 1
    assert D(row['reserved']) == 0 and D(row['free_for_line']) == 0
    assert not row['purchases'][0]['receipts'][0]['local_stock_posted']


@pytest.mark.parametrize('change', ['wrong_material', 'wrong_unit', 'stale_version', 'archive', 'norm_exceeded', 'fractional'])
def test_allocation_rejects_incompatible_sources(native, db, change):
    client, _, raw, product, *_ = native
    oid, _, _ = prepared(native, db)
    supply = purchase(db, raw if change == 'wrong_material' else product, '10')
    if change == 'wrong_unit':
        supply.items = [{**supply.items[0], 'unit': 'м3'}]
    if change == 'archive':
        supply.archived = True
    db.commit()
    response, _ = allocation(client, oid, supply, '3' if change == 'norm_exceeded' else '0.5' if change == 'fractional' else '1',
                              **({'supplier_version': 999} if change == 'stale_version' else {}))
    assert response.status_code in (409, 422), response.text
    assert not db.query(SalesEvent).filter_by(kind='supply_allocated').count()


def test_norm_purchase_only_matches_norm_materials(native, db):
    client, _, raw, product, *_ = native
    oid, _, _ = prepared(native, db, norm=True)
    wrong = purchase(db, product)
    assert allocation(client, oid, wrong)[0].status_code == 409
    proper = purchase(db, raw, '4')
    response, _ = allocation(client, oid, proper, '4')
    assert response.status_code == 200, response.text
    assert journey(client, oid)['lines'][0]['purchases'][0]['material_id'] == raw.id


def test_commercial_ownership_and_financial_redaction(native, db, api, make_user):
    owner_client, _, _, product, *_ = native
    oid, estimate, _ = prepared(native, db)
    supply = purchase(db, product)
    assert allocation(owner_client, oid, supply)[0].status_code == 200
    manager = make_user(Module.CLIENTS)
    assert api(manager).get(f'/api/sales/orders/{oid}/journey').status_code == 404
    stored = db.get(CostingEstimate, estimate['id']); stored.actor_id = manager.id; db.commit()
    client = api(manager)
    response = client.get(f'/api/sales/orders/{oid}/journey')
    assert response.status_code == 200, response.text
    for forbidden in ('unit_price', 'total_cost', 'snapshot', 'actual_profit', 'password', 'tax_basis'):
        assert forbidden not in response.text
    assert not response.json()['lines'][0]['can_allocate']
    assert client.get(f'/api/sales/orders/{oid}/supply-options?order_line=0').status_code == 403
    assert client.post(f'/api/sales/orders/{oid}/supply-allocations', json={
        'request_id': str(uuid4()), 'version': 2, 'order_line': 0, 'supplier_order_id': supply.id,
        'supplier_version': supply.version, 'supplier_line': 0, 'quantity': '1'}).status_code == 403
    assert api(make_user()).get(f'/api/sales/orders/{oid}/journey').status_code == 403


def test_cancelled_and_archived_orders_never_offer_execution(native, db):
    client = native[0]
    oid, _, _ = prepared(native, db)
    row = db.get(SalesOrder, oid); row.status = 'cancelled'; db.commit()
    assert journey(client, oid)['lines'][0]['next']['code'] == 'stopped'
    assert not journey(client, oid)['lines'][0]['can_allocate']
    row.status = 'confirmed'; row.archived = True; db.commit()
    assert journey(client, oid)['lines'][0]['next']['code'] == 'stopped'
