from datetime import datetime, timezone
from uuid import uuid4

from app.accounting.models import Counterparty
from app.common.module_access import Module
from app.moysklad.models import SourceLink, SourceRecord
from app.purchasing.models import PurchaseReceipt, PurchaseReturn
from test_purchasing_workflows import setup, receipt


def test_receipt_register_uses_linked_documents_and_keeps_unknown_payment_empty(db, api, make_user):
    client, _, supplier, material, order = setup(db, api, make_user)
    response = client.post('/api/purchasing/receipts', json=receipt(order, material))
    assert response.status_code == 201, response.text
    current = db.get(PurchaseReceipt, response.json()['id'])
    counterparty = Counterparty(name='Карточка поставщика', supplier_id=current.supplier_id)
    db.add(counterparty)
    db.flush()

    fields = {column.name: getattr(current, column.name)
              for column in PurchaseReceipt.__table__.columns if column.name != 'id'}
    fields.update(number='П-история', source_id='receipt-doc', request_id=None, order_id=None)
    imported = PurchaseReceipt(**fields)
    db.add(imported)
    for kind, identity, title, payload in [
        ('store', 'store-doc', 'Склад в приёмке', {}),
        ('organization', 'company-doc', 'Организация в приёмке', {}),
        ('currency', 'currency-doc', 'Рубль', {'isoCode': 'RUB'}),
        ('supply', 'receipt-doc', 'П-история', {
            'store': {'id': 'store-doc'}, 'organization': {'id': 'company-doc'},
            'agent': {'id': 'agent-doc'}, 'rate': {'currency': {'id': 'currency-doc'}},
            'sum': 12475, 'payedSum': 7505, 'incomingNumber': 'И-007', 'incomingDate': '2026-11-18 10:00:00',
        }),
    ]:
        db.add(SourceRecord(entity_type=kind, source_id=identity, title=title, search_text=title,
                            payload=payload, state='Сверена' if kind == 'supply' else None))
    db.add(SourceLink(source_key='counterparty/agent-doc', target_type='counterparties', target_id=counterparty.id))
    db.commit()

    response = client.get('/api/purchasing/receipts')
    assert response.status_code == 200, response.text
    items = {row['id']: row for row in response.json()['items']}
    assert items[current.id]['counterparty_id'] == counterparty.id
    assert items[current.id]['warehouse'] == material.warehouse.value
    assert items[current.id]['paid'] is None
    source = items[imported.id]
    assert source['counterparty_id'] == counterparty.id
    assert source['warehouse'] == 'Склад в приёмке'
    assert source['organization_name'] == 'Организация в приёмке'
    assert (source['amount'], source['paid'], source['currency']) == ('124.75', '75.05', 'RUB')
    assert (source['incoming_number'], source['incoming_date'], source['status_label']) == (
        'И-007', '2026-11-18', 'Сверена',
    )


def test_receipt_and_return_sort_applies_before_pagination(db, api, make_user):
    client, _, _, material, order = setup(db, api, make_user)
    attached = client.post('/api/purchasing/receipts', json=receipt(order, material)).json()
    returned = client.post('/api/purchasing/returns', json={
        'request_id': str(uuid4()), 'receipt_id': attached['id'],
        'occurred_at': '2026-10-30T13:00:00+03:00', 'note': 'Регрессия сортировки',
        'lines': [{'receipt_line': 0, 'quantity': 1}],
    })
    assert returned.status_code == 201, returned.text
    source_receipt = db.get(PurchaseReceipt, attached['id'])
    source_return = db.get(PurchaseReturn, returned.json()['id'])
    for model, original in [(PurchaseReceipt, source_receipt), (PurchaseReturn, source_return)]:
        original.number = 'Я'
        values = {column.name: getattr(original, column.name) for column in model.__table__.columns if column.name != 'id'}
        values.update(number='А', occurred_at=datetime(2026, 1, 2, tzinfo=timezone.utc))
        for key in ('request_id', 'source_id'):
            if key in values:
                values[key] = str(uuid4()) if key == 'request_id' else None
        db.add(model(**values))
    db.commit()
    for endpoint in ('receipts', 'returns'):
        path = '/api/purchasing/' + endpoint
        for sort in ('oldest', 'number'):
            r = client.get(path, params={'sort': sort, 'limit': 1})
            assert r.status_code == 200, r.text
            assert r.json()['items'][0]['number'] == 'А'
        r = client.get(path, params={'sort': 'newest', 'limit': 1})
        assert r.json()['items'][0]['number'] == 'Я'


def test_received_factures_sort_by_document_date_and_keep_finance_scope(db, api, make_user):
    from app.common.module_access import AccessLevel
    accountant = make_user(Module.ACCOUNTING, level=AccessLevel.VIEW)
    warehouse = make_user(Module.WAREHOUSE, level=AccessLevel.VIEW)
    client = api(accountant)
    path = '/api/moysklad/records'
    assert client.get(path, params={'entity_type': 'facturein'}).json() == {'total': 0, 'items': []}
    for title, date in [('А', '2026-11-18 12:00:00'), ('Б', '2026-11-19 12:00:00'), ('В', None)]:
        db.add(SourceRecord(entity_type='facturein', source_id=str(uuid4()), title=title,
                            search_text=title.lower(), payload={'moment': date}))
    db.commit()
    r = client.get(path, params={'entity_type': 'facturein', 'sort': 'newest', 'offset': 1, 'limit': 1})
    assert r.status_code == 200, r.text
    assert r.json()['items'][0]['title'] == 'А'
    assert api(warehouse).get(path, params={'entity_type': 'facturein'}).status_code == 403
