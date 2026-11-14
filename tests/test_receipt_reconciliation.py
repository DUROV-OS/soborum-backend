from copy import deepcopy
from uuid import uuid4
import pytest
from app.common.module_access import Module, AccessLevel
from app.costing.governance_models import CostingApprovalPolicy
from app.moysklad.models import SourceRecord, SourceLink
from app.purchasing.models import PurchaseReceipt
from app.purchasing.control_models import PurchaseControlRecord
from app.warehouse.models import StockMovement, StockMovementReason
from app.common.files import FileAsset
from tests.test_purchasing_editing import received, correction, P
from tests.test_purchase_controls import upload


def source(db, kind, id, payload):
    row = SourceRecord(entity_type=kind, source_id=id, title='Документ ' + id, search_text='', payload=payload)
    db.add(row); db.flush(); return row


@pytest.fixture
def imported(db, api, make_user):
    c, owner, supplier, material, order, receipt, _ = received(db, api, make_user)
    accountant = make_user(Module.ACCOUNTING)
    db.add(CostingApprovalPolicy(id=1, director_user_id=owner.id, accounting_user_id=accountant.id))
    base = db.get(PurchaseReceipt, receipt['id']); base.operation_id = None; base.source_id = 'receipt-original'
    for movement in db.query(StockMovement).all(): db.delete(movement)
    db.add(StockMovement(warehouse_material_id=material.id, delta=13, balance_after=13, reason=StockMovementReason.MANUAL_ADJUST, created_by_id=owner.id, note='Сверка остатков с МойСкладом; дата переноса'))
    db.add(SourceLink(source_key='stock/product-one/store-one', target_type='warehouse_materials', target_id=material.id))
    original = source(db, 'supply', base.source_id, {'applicable': True, 'store': {'id': 'store-one'}, 'moment': '2026-10-29 10:00:00',
        'positions': [{'assortment': {'id': 'product-one'}, 'quantity': 3, 'price': 2000}]})
    report = source(db, 'report/stock/store/store-one', 'product-one', {'stock': 13, 'price': 1230.769231})
    db.commit()
    return owner, accountant, supplier, material, order, base, original, report


def confirmation(state, evidence, **extra):
    return {'request_id': str(uuid4()), 'fingerprint': state['fingerprint'], 'evidence': evidence,
            'reason': 'Сверено с актом и ведомостью стоимости', 'confirmed': True, **extra}


def approve(api, accountant, supplier, base):
    c = api(accountant); path = f'{P}/receipts/{base.id}/reconciliation'
    state = c.get(path).json(); assert not state['issues'], state
    proof = upload(c, supplier.id)
    body = confirmation(state, proof)
    result = c.post(path, json=body)
    assert result.status_code == 200, result.text
    assert result.json()['approved']
    return result.json(), proof, body


def test_accountant_reconciles_director_corrects_and_chain_can_be_rechecked(imported, db, api):
    owner, accountant, supplier, material, order, base, original, report = imported
    approval, proof, body = approve(api, accountant, supplier, base)
    c = api(accountant); path = f'{P}/receipts/{base.id}'
    assert c.post(path + '/reconciliation', json=body).status_code == 200
    assert db.query(PurchaseControlRecord).filter_by(kind='receipt_reconcile').count() == 1
    row = c.get(path).json()
    change = correction(row, lines=[{'quantity': '4', 'unit_price': '25'}], evidence=proof, reconciliation_id=approval['approval_id'])
    assert c.patch(path, json=change).status_code == 403
    c = api(owner)
    result = c.patch(path, json=change)
    assert result.status_code == 200, result.text
    db.refresh(material); assert material.quantity_in_stock == 14
    assert not c.get(path + '/reconciliation').json()['approved']
    assert not c.get(path + '/reconciliation').json()['issues']
    assert c.patch(path, json=change).status_code == 200
    assert original.payload['positions'][0]['quantity'] == 3
    assert report.payload['stock'] == 13
    approval2, proof2, _ = approve(api, accountant, supplier, base)
    c = api(owner)
    next_change = correction(c.get(path).json(), lines=[{'quantity': '3', 'unit_price': '22'}], evidence=proof2, reconciliation_id=approval2['approval_id'])
    result = c.patch(path, json=next_change)
    assert result.status_code == 200, result.text
    db.refresh(material); assert material.quantity_in_stock == 13
    assert c.get(f'{P}/orders/{order["id"]}').json()['items'][0]['received_quantity'] == 3


@pytest.mark.parametrize('issue', ['consumed', 'incomplete', 'invalid_date', 'stock_changed', 'price_changed', 'source_changed', 'native_movement', 'missing_report', 'wrong_unit'])
def test_uncertain_basis_cannot_be_signed(imported, db, api, issue):
    owner, accountant, supplier, material, _, base, original, report = imported
    if issue == 'consumed': source(db, 'loss', 'loss-one', {'applicable': True, 'moment': '2026-10-30 00:00:00', 'positions': [{'assortment': {'id': 'product-one'}, 'quantity': 1}]})
    if issue == 'incomplete': source(db, 'loss', 'loss-one', {'applicable': True, 'moment': '2026-10-30 00:00:00', 'positions': {'meta': {'size': 2}, 'rows': []}})
    if issue == 'invalid_date': source(db, 'loss', 'loss-one', {'applicable': True, 'moment': 'unknown', 'positions': []})
    if issue == 'stock_changed': material.quantity_in_stock = 12
    if issue == 'price_changed': material.purchase_price = 99
    if issue == 'wrong_unit': material.unit = 'шт'
    if issue == 'source_changed': original.payload = {**original.payload, 'positions': [{'assortment': {'id': 'product-one'}, 'quantity': 4, 'price': 2000}]}
    if issue == 'native_movement': db.add(StockMovement(warehouse_material_id=material.id, delta=-1, reason=StockMovementReason.ISSUED_MANUAL, created_by_id=owner.id))
    if issue == 'missing_report': db.delete(report)
    db.commit()
    c = api(accountant); path = f'{P}/receipts/{base.id}/reconciliation'
    state = c.get(path).json(); assert state['issues'], state
    response = c.post(path, json=confirmation(state, upload(c, supplier.id)))
    assert response.status_code == 409, response.text
    assert db.query(PurchaseControlRecord).filter_by(kind='receipt_reconcile').count() == 0


def test_assignment_proof_integrity_and_stale_basis(imported, db, api, make_user):
    owner, accountant, supplier, material, _, base, _, report = imported
    path = f'{P}/receipts/{base.id}/reconciliation'
    c = api(owner); state = c.get(path).json(); proof = upload(c, supplier.id)
    assert c.post(path, json=confirmation(state, proof)).status_code == 403
    c = api(accountant)
    assert c.post(path, json=confirmation(state, proof, confirmed=False)).status_code == 422
    report.payload = {**report.payload, 'stock': 12}; db.commit()
    assert c.post(path, json=confirmation(state, proof)).status_code == 409
    report.payload = {**report.payload, 'stock': 13}; db.commit()
    approval, proof, _ = approve(api, accountant, supplier, base)
    from pathlib import Path
    Path(db.get(FileAsset, proof['file_id']).path_on_disk).write_bytes(b'damaged')
    c = api(owner)
    state = c.get(path).json(); assert not state['approved']
    assert api(make_user(Module.WAREHOUSE, level=AccessLevel.VIEW)).get(path).status_code == 403


def test_new_source_operation_revokes_approval_without_stock_write(imported, db, api):
    owner, accountant, supplier, material, _, base, _, _ = imported
    approval, proof, _ = approve(api, accountant, supplier, base)
    source(db, 'productionstagecompletion', 'production-one', {'applicable': True, 'moment': '2026-10-31 00:00:00', 'materials': [{'assortment': {'id': 'product-one'}, 'quantity': 1}]}); db.commit()
    c = api(owner); path = f'{P}/receipts/{base.id}'
    change = correction(c.get(path).json(), lines=[{'quantity': '4', 'unit_price': '25'}], evidence=proof, reconciliation_id=approval['approval_id'])
    assert c.patch(path, json=change).status_code == 409
    db.refresh(material); assert material.quantity_in_stock == 13
    assert db.query(PurchaseControlRecord).filter_by(kind='receipt_edit').count() == 0
