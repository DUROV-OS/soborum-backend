from copy import deepcopy
from datetime import datetime, timezone, timedelta
from io import BytesIO
from pathlib import Path
from uuid import uuid4
import pytest
from PIL import Image
from app.accounting.models import SupplierOrder, MoneyMovement
from app.common.files import FileAsset
from app.common.module_access import Module
from app.purchasing import controls as c
from app.purchasing.control_models import PurchaseControlRecord as Record
from app.purchasing.models import PurchaseReceipt
from app.sales.models import SalesOrder
from app.warehouse.models import Supplier, StockMovement
from tests.test_costing_integration import native
from tests.test_order_journey import prepared, purchase, allocation

P = '/api/purchasing'


def upload(client, sid):
    out = BytesIO(); Image.new('RGB', (4, 4), 'white').save(out, format='PNG')
    response = client.post(f'{P}/suppliers/{sid}/documents', files={'file': ('scan.png', out.getvalue(), 'image/png')})
    assert response.status_code == 201, response.text
    return {'file_id': response.json()['file_id'], 'locator': 'Страница 1, строка 1: цена и условия'}


def terms(evidence, **extra):
    return {'vat': 'included', 'vat_rate': '20', 'delivery_per_unit': '0', 'destination': 'Цех компании',
            'payment_terms': 'Предоплата 100%', 'min_quantity': '1', 'max_quantity': '100',
            'evidence': evidence, **extra}


def price_body(row, proof, **extra):
    return {'request_id': str(uuid4()), 'expected_id': 0, 'version': row.version, 'number': 'СЧ-1',
            'document_date': c.today().isoformat(), 'valid_until': (c.today() + timedelta(days=30)).isoformat(),
            'lines': [dict(index=i, **terms(proof)) for i in range(len(row.items))], **extra}


def ready(native, db, api):
    client, owner, raw, *_ = native
    oid, _, _ = prepared(native, db, norm=True)
    row = purchase(db, raw, '4')
    assert allocation(client, oid, row, '4')[0].status_code == 200
    previous = SupplierOrder(supplier_id=row.supplier_id, number='ЗП-прошлый', items=deepcopy(row.items), total_cost=200)
    db.add(previous); db.flush()
    receipt = PurchaseReceipt(order_id=previous.id, supplier_id=row.supplier_id, actor_id=owner.id, number='ПР-1',
        occurred_at=datetime.now(timezone.utc) - timedelta(days=1), status='posted',
        lines=[{'order_line': 0, 'material_id': raw.id, 'quantity': 4, 'unit_price': 50}])
    db.add(receipt); db.commit()
    proof = upload(client, row.supplier_id)
    trust_body = {'request_id': str(uuid4()), 'expected_id': 0, 'trusted': True, 'evidence': proof, 'reason': 'Проверены договор и реквизиты'}
    assert client.post(f'{P}/suppliers/{row.supplier_id}/trust', json=trust_body).status_code == 200
    accountant = native[0].approvals.f['accountant']
    accounting_client = api(accountant)
    for order, args in [(previous, {'receipt_id': receipt.id, 'receipt_evidence': proof}), (row, {})]:
        response = accounting_client.post(f'{P}/orders/{order.id}/price-evidence', json=price_body(order, proof, **args))
        assert response.status_code == 200, response.text
    client = api(owner)
    return client, row, previous, receipt, proof, oid, accountant


def review(client, row):
    response = client.get(f'{P}/orders/{row.id}/automatic-review')
    assert response.status_code == 200, response.text
    return response.json()


def test_document_chain_approves_once_without_stock_or_money(native, db, api):
    client, row, previous, receipt, proof, oid, accountant = ready(native, db, api)
    data = review(client, row)
    assert data['decision'] == 'eligible', data
    assert data['lines'][0]['previous']['order_id'] == previous.id
    assert data['lines'][0]['targets'][0]['order_id'] == oid
    body = {'request_id': str(uuid4()), 'fingerprint': data['fingerprint']}
    path = f'{P}/orders/{row.id}/automatic-approval'
    response = client.post(path, json=body)
    assert response.status_code == 200, response.text
    assert response.json()['review']['decision'] == 'auto_approved'
    assert client.post(path, json=body).json()['id'] == response.json()['id']
    assert client.post(path, json={**body, 'fingerprint': '0' * 64}).status_code == 409
    assert db.query(Record).filter_by(kind='approval').count() == 1
    assert db.query(MoneyMovement).count() == db.query(StockMovement).count() == 0
    assert not data['auto_payment_enabled']


@pytest.mark.parametrize('change', ['price', 'reverted_price', 'contacts', 'scan', 'sale', 'receipt', 'newer_unverified', 'norm'])
def test_changed_sources_never_keep_approval(native, db, api, change):
    client, row, previous, receipt, proof, oid, accountant = ready(native, db, api)
    data = review(client, row)
    body = {'request_id': str(uuid4()), 'fingerprint': data['fingerprint']}
    assert client.post(f'{P}/orders/{row.id}/automatic-approval', json=body).status_code == 200
    if change == 'price':
        row.items = [{**row.items[0], 'unit_price': 51}]
    elif change == 'reverted_price':
        row.version += 2  # price edited, then restored: the old personal check cannot revive
    elif change == 'contacts':
        db.get(Supplier, row.supplier_id).contacts = [{'kind': 'email', 'value': 'changed@test.invalid'}]
    elif change == 'scan':
        Path(db.get(FileAsset, proof['file_id']).path_on_disk).write_bytes(b'damaged')
    elif change == 'sale':
        db.get(SalesOrder, oid).status = 'cancelled'
    elif change == 'receipt':
        receipt.lines = [{**receipt.lines[0], 'quantity': 3}]
    elif change == 'newer_unverified':
        db.add(PurchaseReceipt(order_id=previous.id, supplier_id=row.supplier_id, actor_id=native[1].id, number='Позже',
            occurred_at=datetime.now(timezone.utc), status='posted', lines=deepcopy(receipt.lines)))
    else:
        from app.costing.models import CostingOrderLink
        from app.moysklad.recipes import Recipe
        link = db.query(CostingOrderLink).filter_by(order_id=oid).one()
        db.get(Recipe, link.recipe_id).version += 1
    db.commit()
    data = review(client, row)
    assert data['decision'] == 'manual_review', data
    assert data['previous_approval_id'] and not data['approval_id']
    assert client.post(f'{P}/orders/{row.id}/automatic-approval', json={**body, 'request_id': str(uuid4())}).status_code == 409


@pytest.mark.parametrize('field,value', [('delivery_per_unit', '1'), ('vat_rate', '22'), ('payment_terms', 'Отсрочка 30 дней'),
                                        ('max_quantity', '3'), ('destination', 'Другой город')])
def test_price_conditions_are_not_reduced_to_nominal_total(native, db, api, field, value):
    client, row, previous, receipt, proof, oid, accountant = ready(native, db, api)
    body = price_body(row, proof, expected_id=review(client, row)['price']['id'])
    body['lines'][0][field] = value
    response = api(accountant).post(f'{P}/orders/{row.id}/price-evidence', json=body)
    if field == 'max_quantity':
        assert response.status_code == 422
    else:
        assert response.status_code == 200, response.text
        data = review(api(native[1]), row)
        assert data['decision'] == 'manual_review'
        assert data['lines'][0]['issues']


def test_named_people_and_document_scope(native, db, api, make_user):
    client, row, previous, receipt, proof, oid, accountant = ready(native, db, api)
    stranger = api(make_user(admin=True))
    assert stranger.post(f'{P}/orders/{row.id}/price-evidence', json=price_body(row, proof)).status_code == 403
    assert stranger.post(f'{P}/suppliers/{row.supplier_id}/trust', json={
        'request_id': str(uuid4()), 'expected_id': 0, 'trusted': True, 'evidence': proof, 'reason': 'Проверено'}).status_code == 403
    other = Supplier(name='Другой поставщик'); db.add(other); db.commit()
    assert stranger.get(f'{P}/suppliers/{other.id}/documents/{proof["file_id"]}').status_code == 404
    assert stranger.get(f'/files/{proof["file_id"]}').status_code == 403
    assert api(make_user(Module.WAREHOUSE)).get(f'{P}/orders/{row.id}/automatic-review').status_code == 403


def test_market_quotes_require_recent_comparable_signed_documents(native, db, api):
    client, row, previous, receipt, proof, oid, accountant = ready(native, db, api)
    other = Supplier(name='Конкурирующий поставщик'); db.add(other); db.commit()
    client = api(accountant); evidence = upload(client, other.id)
    body = dict(request_id=str(uuid4()), material_id=native[2].id, unit=native[2].unit, currency='RUB',
                unit_price='40', number='КП-1', document_date=c.today().isoformat(),
                valid_until=(c.today() + timedelta(days=30)).isoformat(), **terms(evidence))
    path = f'{P}/suppliers/{other.id}/offers'
    response = client.post(path, json=body)
    assert response.status_code == 200, response.text
    assert client.post(path, json=body).json() == response.json()
    line = review(client, row)['lines'][0]
    assert not line['market_review_due'] and line['offers'][0]['delivered_unit_price'] == '40'
    expired = {**body, 'request_id': str(uuid4()), 'document_date': (c.today()-timedelta(days=15)).isoformat()}
    assert client.post(path, json=expired).status_code == 200
    line = review(client, row)['lines'][0]
    assert line['market_review_due'] and not line['offers']  # superseded quote cannot revive


def test_missing_facts_are_explicit_and_not_fabricated(native, db):
    row = purchase(db, native[2])
    data = review(native[0], row)
    assert data['decision'] == 'manual_review'
    assert len(data['reasons']) >= 3 and data['lines'][0]['previous'] is None
    assert not db.query(Record).count()


def test_approved_new_revision_cannot_validate_old_sale_link(native, db, api, monkeypatch):
    from app.costing.models import CostingEstimate, CostingOrderLink
    client, row, previous, receipt, proof, oid, accountant = ready(native, db, api)
    link = db.query(CostingOrderLink).filter_by(order_id=oid).one()
    estimate = db.get(CostingEstimate, link.estimate_id)
    estimate.version += 1; db.commit()
    # Even if the newest calculation is approved, it does not validate an old pinned norm.
    monkeypatch.setattr(c.governance, 'assert_approved', lambda *args: None)
    data = review(client, row)
    assert data['decision'] == 'manual_review'
    assert any('прежней версией' in text for text in data['lines'][0]['issues'])


@pytest.mark.parametrize('actual', [30, None, 0])
def test_receipt_price_cannot_be_replaced_by_order_price(native, db, api, actual):
    client, row, previous, receipt, proof, oid, accountant = ready(native, db, api)
    receipt.lines = [{**receipt.lines[0], 'unit_price': actual}]
    # Simulate previously attested/imported contradictory evidence. Freshness alone
    # must not make a nominal order price equivalent to the actual received price.
    checked = c.latest(db, 'price', previous.supplier_id, previous.id)
    checked.content = {**checked.content, 'receipt_basis': c.receipt_basis(receipt)}
    db.commit()
    data = review(client, row)
    assert data['decision'] == 'manual_review'
    assert any('Фактическая цена' in issue for issue in data['lines'][0]['issues'])
    response = api(accountant).post(f'{P}/orders/{previous.id}/price-evidence', json=price_body(
        previous, proof, expected_id=checked.id, receipt_id=receipt.id, receipt_evidence=proof))
    assert response.status_code == 422
