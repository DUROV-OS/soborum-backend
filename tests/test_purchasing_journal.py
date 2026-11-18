from datetime import datetime, timezone, timedelta
from app.accounting.models import SupplierOrder, SupplierOrderStatus, MoneyMovement, MoneyMovementStatus
from app.purchasing.models import PurchaseReturn
from test_purchasing_workflows import setup, receipt


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
