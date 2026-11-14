from copy import deepcopy
from decimal import Decimal
from datetime import datetime, timezone
from uuid import uuid4
import pytest
from tests.test_purchasing_workflows import setup, receipt
from tests.test_purchase_controls import upload
from app.purchasing.control_models import PurchaseControlRecord
from app.purchasing.models import PurchaseReceipt
from app.warehouse.models import StockMovement, StockMovementReason
from app.common.module_access import Module, AccessLevel

P = '/api/purchasing'


def received(db, api, make_user):
    client, owner, supplier, material, order = setup(db, api, make_user)
    original = receipt(order, material)
    response = client.post(P + '/receipts', json=original)
    assert response.status_code == 201, response.text
    row = client.get(P + '/receipts/' + str(response.json()['id'])).json()
    return client, owner, supplier, material, order, row, original


def correction(row, **extra):
    at = datetime.fromisoformat(row['occurred_at'])
    if at.tzinfo is None: at = at.replace(tzinfo=timezone.utc)
    return {'request_id': str(uuid4()), 'edit_token': row['edit_token'], 'number': row['number'],
            'occurred_at': at.isoformat(), 'note': 'Уточнение', 'reason': 'Исправлена ошибка ввода',
            'lines': [{'quantity': str(l['quantity']), 'unit_price': str(l['unit_price'])} for l in row['lines']], **extra}


def test_order_edit_after_receipt_keeps_actual_stock_and_version(db, api, make_user):
    c, _, _, m, order, row, _ = received(db, api, make_user)
    order = c.get(f'{P}/orders/{order["id"]}').json()
    assert order['has_receipts']
    items = deepcopy(order['items']); items[0].update(quantity=4, unit_price=25)
    path = f'{P}/orders/{order["id"]}?version={order["version"]}'
    result = c.patch(path, json={'number': 'Уточнённый', 'items': items, 'reason': 'Новый счёт'})
    assert result.status_code == 200, result.text
    assert result.json()['total_cost'] == 100
    db.refresh(m); assert m.quantity_in_stock == 13
    assert c.get(f'{P}/receipts/{row["id"]}').json()['lines'][0]['unit_price'] == 20
    assert c.patch(path, json={'comment': 'Старая форма'}).status_code == 409
    assert db.query(PurchaseControlRecord).filter_by(kind='order_edit').count() == 1
    new = result.json(); items[0]['quantity'] = 2
    assert c.patch(f'{P}/orders/{order["id"]}?version={new["version"]}', json={'items': items}).status_code == 409


def test_completed_order_can_be_extended_and_supplier_identity_protected(db, api, make_user):
    c, _, supplier, m, order = setup(db, api, make_user)
    assert c.post(P + '/receipts', json=receipt(order, m, '10')).status_code == 201
    order = c.get(f'{P}/orders/{order["id"]}').json()
    items = deepcopy(order['items']); items[0]['quantity'] = 12
    r = c.patch(f'{P}/orders/{order["id"]}?version={order["version"]}', json={'items': items})
    assert r.status_code == 200, r.text
    assert r.json()['status'] == 'in_transit'
    assert c.patch(f'{P}/orders/{order["id"]}?version={r.json()["version"]}', json={'supplier_id': supplier.id + 100}).status_code == 409


def test_receipt_correction_delta_evidence_retry_and_original_retry(db, api, make_user):
    c, owner, supplier, m, order, row, original = received(db, api, make_user)
    proof = upload(c, supplier.id)
    body = correction(row, lines=[{'quantity': '4', 'unit_price': '25'}], evidence=proof)
    path = f'{P}/receipts/{row["id"]}'
    r = c.patch(path, json=body)
    assert r.status_code == 200, r.text
    db.refresh(m)
    assert m.quantity_in_stock == 14
    assert abs(Decimal(str(m.purchase_price)) - Decimal(200) / 14) < Decimal('.00000002')
    assert c.get(f'{P}/orders/{order["id"]}').json()['items'][0]['received_quantity'] == 4
    count = db.query(StockMovement).count()
    assert c.patch(path, json=body).status_code == 200
    assert c.post(P + '/receipts', json=original).status_code == 201
    assert db.query(StockMovement).count() == count
    assert len(c.get(path + '/history').json()) == 1
    assert c.patch(path, json=correction(row)).status_code == 409
    assert c.patch(path, json={**body, 'reason': 'Другие данные'}).status_code == 409


@pytest.mark.parametrize('case', ['no_proof', 'too_many', 'consumed', 'imported', 'wrong_supplier_proof'])
def test_rejected_receipt_correction_is_atomic(db, api, make_user, case):
    c, owner, supplier, m, order, row, _ = received(db, api, make_user)
    proof = upload(c, supplier.id)
    if case == 'wrong_supplier_proof':
        from app.warehouse.models import Supplier
        other = Supplier(name='Другой', contacts=[], categories=[]); db.add(other); db.commit()
        proof = upload(c, other.id)
    if case == 'consumed':
        db.add(StockMovement(warehouse_material_id=m.id, delta=-1, reason=StockMovementReason.ISSUED_MANUAL, created_by_id=owner.id)); db.commit()
    if case == 'imported':
        base = db.get(PurchaseReceipt, row['id']); base.operation_id = None; base.source_id = 'source'; db.commit()
    body = correction(row, lines=[{'quantity': '11' if case == 'too_many' else '4', 'unit_price': '25'}], evidence=None if case == 'no_proof' else proof)
    response = c.patch(f'{P}/receipts/{row["id"]}', json=body)
    assert response.status_code in (409, 422), response.text
    db.refresh(m); assert m.quantity_in_stock == 13
    assert db.query(PurchaseControlRecord).filter_by(kind='receipt_edit').count() == 0
    assert c.get(f'{P}/receipts/{row["id"]}').json()['lines'] == row['lines']


def test_metadata_imported_receipt_and_readonly_permissions(db, api, make_user):
    c, _, _, m, order, row, _ = received(db, api, make_user)
    base = db.get(PurchaseReceipt, row['id']); base.operation_id = None; base.source_id = 'import'; db.commit()
    body = correction(row, number='Исправлен номер')
    assert c.patch(f'{P}/receipts/{row["id"]}', json=body).status_code == 200
    read_only = api(make_user(Module.ACCOUNTING, level=AccessLevel.VIEW))
    assert read_only.patch(f'{P}/receipts/{row["id"]}', json=body).status_code == 403
    db.refresh(m); assert m.quantity_in_stock == 13


def test_material_edit_stale_form_duplicate_and_validation(db, api, make_user):
    c, _, _, m, _ = setup(db, api, make_user)
    path = f'/api/warehouse/materials/{m.id}'
    old = c.get(path).json()
    result = c.patch(path, json={'title': 'Брус сухой', 'code': 'NEW-CODE', 'threshold': 2, 'edit_token': old['edit_token']})
    assert result.status_code == 200, result.text
    assert result.json()['title'] == 'Брус сухой'
    db.expire_all()
    assert c.patch(path, json={'threshold': 3, 'edit_token': result.json()['edit_token']}).status_code == 200
    assert c.patch(path, json={'title': 'Потеря изменений', 'edit_token': old['edit_token']}).status_code == 409
    assert c.patch(path, json={'title': '  '}).status_code == 422
    assert c.patch(path, json={'threshold': -1}).status_code == 422
    assert c.patch(path, json={'purchase_price': -1}).status_code == 422
    db.refresh(m); assert m.quantity_in_stock == 10


def test_source_record_links_to_editable_card_and_preserves_original(db, api, make_user):
    from app.moysklad.models import SourceRecord, SourceLink
    c, _, _, m, order = setup(db, api, make_user)
    source = SourceRecord(entity_type='product', source_id='test-product', title='Исходный товар', search_text='товар', payload={'name': 'Исходный товар'})
    db.add(source); db.flush()
    db.add(SourceLink(source_key='stock/test-product/store1', target_type='warehouse_materials', target_id=m.id)); db.commit()
    path = f'/api/moysklad/records/{source.id}'
    detail = c.get(path).json()
    assert detail['working_links'][0]['url'] == f'/warehouse?tab=materials&material={m.id}'
    assert c.patch(f'/api/warehouse/materials/{m.id}', json={'title': 'Рабочее название'}).status_code == 200
    assert c.get(path).json()['payload']['name'] == 'Исходный товар'
    reader = api(make_user(Module.ACCOUNTING))
    assert reader.get(path).status_code == 403


def test_quantity_correction_cannot_bypass_return(db, api, make_user):
    c, owner, supplier, m, order, row, _ = received(db, api, make_user)
    r = c.post(P + '/returns', json={'request_id': str(uuid4()), 'receipt_id': row['id'],
        'occurred_at': '2026-10-30T10:00:00+03:00', 'note': 'Брак',
        'lines': [{'receipt_line': 0, 'quantity': '1', 'material_id': m.id}]})
    assert r.status_code == 201, r.text
    proof = upload(c, supplier.id)
    body = correction(row, lines=[{'quantity': '4', 'unit_price': '20'}], evidence=proof)
    response = c.patch(f'{P}/receipts/{row["id"]}', json=body)
    assert response.status_code == 409, response.text
    assert 'возврат' in response.json()['detail'].lower()
    db.refresh(m); assert m.quantity_in_stock == 12
