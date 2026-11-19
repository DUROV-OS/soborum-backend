from datetime import datetime, timezone, timedelta
from app.accounting.models import Organization, SupplierOrder, SupplierOrderStatus, MoneyMovement, MoneyMovementStatus
from app.purchasing.models import PurchaseReceipt, PurchaseReturn
from app.moysklad.models import SourceRecord
from test_purchasing_workflows import setup, receipt


def test_order_journal_totals_posted_receipts_at_actual_prices(db, api, make_user):
    client, owner, supplier, material, order = setup(db, api, make_user)
    path = '/api/purchasing/orders'
    assert client.get(path).json()['items'][0]['received_amount'] == '0.00'
    for quantity, price in [('2.5', '23.40'), ('1', '17.65')]:
        current = client.get(f'{path}/{order["id"]}').json()
        body = receipt(current, material, quantity=quantity)
        body['lines'][0]['unit_price'] = price
        response = client.post('/api/purchasing/receipts', json=body)
        assert response.status_code == 201, response.text
    for status, order_id in [('draft', order['id']), ('posted', None)]:
        db.add(PurchaseReceipt(
            actor_id=owner.id, supplier_id=supplier.id, order_id=order_id, status=status,
            occurred_at=datetime.now(timezone.utc), lines=[{'quantity': 5, 'unit_price': 100}],
        ))
    db.commit()
    row = client.get(path).json()['items'][0]
    assert row['received_amount'] == '76.15'
    assert row['total_cost'] == 200


def test_purchase_order_organization_is_selected_from_company_directory(db, api, make_user):
    client, _, supplier, material, order = setup(db, api, make_user)
    tech = db.query(Organization).filter_by(short_name='Технология').one()
    group = db.query(Organization).filter_by(short_name='ИД Групп').one()
    other = Organization(name='Другая компания', short_name='Другая компания')
    db.add(other)
    db.commit()
    catalog = client.get('/api/purchasing/catalog').json()
    assert catalog['organizations'] == [{'id': tech.id, 'name': 'Технология'}, {'id': group.id, 'name': 'ИД Групп'}]
    body = {'supplier_id': supplier.id, 'items': order['items']}
    path = '/api/purchasing/orders'
    assert client.post(path, json={**body, 'organization_id': other.id}).status_code == 422
    response = client.post(path, json={**body, 'organization_id': group.id})
    assert response.status_code == 201, response.text
    row = response.json()
    assert row['organization_name'] == 'ИД Групп'
    order_path = f'{path}/{row["id"]}'
    response = client.patch(f'{order_path}?version={row["version"]}',
                            json={'organization_id': tech.id, 'reason': 'Выбран покупатель по заказу'})
    assert response.status_code == 200, response.text
    row = response.json()
    assert row['organization_id'] == tech.id
    assert client.post('/api/purchasing/receipts', json=receipt(row, material)).status_code == 201
    saved = client.get(order_path).json()
    assert client.patch(f'{order_path}?version={saved["version"]}',
                        json={'organization_id': group.id, 'reason': 'Проверка запрета'}).status_code == 409
    listing = {item['id']: item for item in client.get(path).json()['items']}
    assert listing[row['id']]['organization_name'] == 'Технология'
    assert listing[order['id']]['organization_id'] is None


def test_journal_invoices_total_only_issued_documents_linked_to_each_order(db, api, make_user):
    client, _, supplier, _, order = setup(db, api, make_user)
    db.get(SupplierOrder, order['id']).source_id = 'source-order'
    empty_order = SupplierOrder(
        supplier_id=supplier.id, number='Без счёта', source_id='other-order', items=[], total_cost=80,
    )
    db.add(empty_order)
    db.add(SourceRecord(entity_type='currency', source_id='usd', title='USD', search_text='', payload={'isoCode': 'USD'}))

    def invoice(number, amount, *, linked='source-order', applicable=True, archived=False, kind='invoicein', currency=None):
        payload = {'sum': amount, 'applicable': applicable, 'incomingNumber': number}
        if linked:
            payload['purchaseOrder'] = {'meta': {'type': 'purchaseorder', 'href': f'https://example.invalid/entity/purchaseorder/{linked}'}}
        if currency:
            payload['rate'] = {'currency': {'id': currency}}
        db.add(SourceRecord(entity_type=kind, source_id=number, title=number, search_text='', payload=payload, archived=archived))

    invoice('A', 12345)
    invoice('B', 10055.0)
    invoice('C', 4000, currency='usd')
    invoice('draft', 8000, applicable=False)
    invoice('archived', 8000, archived=True)
    invoice('unlinked', 8000, linked=None)
    invoice('separate', 8000, linked='third-order')
    invoice('facture', 8000, kind='facturein')
    db.commit()

    response = client.get('/api/purchasing/orders')
    assert response.status_code == 200, response.text
    rows = {row['id']: row for row in response.json()['items']}
    assert rows[order['id']]['invoice_totals'] == [
        {'currency': 'RUB', 'amount': '224.00'}, {'currency': 'USD', 'amount': '40.00'},
    ]
    assert rows[order['id']]['unpaid'] == 200
    assert rows[empty_order.id]['invoice_totals'] == []
    documents = client.get(f'/api/purchasing/orders/{order["id"]}/documents').json()
    assert [invoice['number'] for invoice in documents['invoices']] == ['A', 'B', 'C']


def test_journal_filters_totals_and_pagination(db, api, make_user):
    client, owner, supplier, material, order = setup(db, api, make_user)
    base = db.get(SupplierOrder, order['id'])
    base.number = 'A'; base.comment = 'Нужен 100% сухой брус'; base.opening_paid = 50
    base.expected_at = datetime.now(timezone.utc).date() - timedelta(days=2)
    db.add(SupplierOrder(supplier_id=supplier.id, number='B', items=[], total_cost=80, opening_paid=80, status=SupplierOrderStatus.RECEIVED))
    db.add(SupplierOrder(supplier_id=supplier.id, number='C', items=[], total_cost=10, opening_paid=15, archived=True))
    db.commit()
    result = client.get('/api/purchasing/orders?limit=1').json()
    assert result['total'] == 2 and len(result['items']) == 1
    assert result['summary'][0] == dict(currency='RUB', total=280, paid=130, pending=0, net=280, unpaid=150, overpaid=0)
    for query in ('payment=partial', 'delivery=overdue', 'query=100%25', 'query=Брус', f'supplier_id={supplier.id}&sort=oldest&limit=1'):
        r = client.get('/api/purchasing/orders?' + query)
        assert r.status_code == 200, r.text
        assert r.json()['items'][0]['id'] == order['id']
    assert client.get('/api/purchasing/orders?payment=paid').json()['items'][0]['number'] == 'B'
    overpaid = client.get('/api/purchasing/orders?archive=all&payment=overpaid').json()
    assert overpaid['items'][0]['number'] == 'C' and overpaid['summary'][0]['overpaid'] == 5
    totals = client.get('/api/purchasing/orders?archive=all').json()['summary'][0]
    assert totals['net'] + totals['overpaid'] == totals['paid'] + totals['unpaid']
    assert client.get('/api/purchasing/orders?date_from=2026-11-10&date_to=2026-11-01').status_code == 422
    assert client.get('/api/purchasing/orders?sort=unknown').status_code == 422


def test_journal_return_credit_and_pending_are_not_paid(db, api, make_user, default_account):
    client, owner, supplier, material, order = setup(db, api, make_user)
    first = client.post('/api/purchasing/receipts', json=receipt(order, material)).json()
    from uuid import uuid4
    result = client.post('/api/purchasing/returns', json=dict(request_id=str(uuid4()), receipt_id=first['id'],
        occurred_at='2026-11-14T12:00:00+03:00', note='Возврат для проверки', lines=[dict(receipt_line=0,quantity=1)]))
    assert result.status_code == 201, result.text
    base = db.get(SupplierOrder, order['id']); base.opening_paid = 180; db.commit()
    r = client.get('/api/purchasing/orders?payment=paid').json()
    assert r['total'] == 1 and r['summary'][0]['net'] == 180
    assert r['summary'][0]['paid'] == r['items'][0]['paid'] == 180
    base.opening_paid = 0; db.commit()
    result = client.post(f"/api/purchasing/orders/{base.id}/payments", json=dict(request_id=str(uuid4()), amount='50', account_id=default_account.id))
    assert result.status_code == 201, result.text
    r = client.get('/api/purchasing/orders?payment=pending').json()
    assert r['total'] == 1 and r['summary'][0]['pending'] == 50 and r['summary'][0]['paid'] == 0
    assert client.get('/api/purchasing/orders?payment=paid').json()['total'] == 0
    documents = client.get(f'/api/purchasing/orders/{base.id}/documents').json()
    assert [r['id'] for r in documents['receipts']] == [first['id']]
    assert len(documents['returns']) == 1 and documents['payments'][0]['status'] == 'draft'
    assert documents['opening_paid'] == 0
    assert client.get('/api/purchasing/orders/99999/documents').status_code == 404
    assert client.get('/api/purchasing/receipts?query=Поставщик').json()['total'] == 1
    assert client.get('/api/purchasing/receipts?query=Брус').json()['total'] == 1
    assert client.get('/api/purchasing/receipts?query=несуществующий').json()['total'] == 0
