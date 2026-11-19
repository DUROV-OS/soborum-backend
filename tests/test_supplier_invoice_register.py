from app.common.module_access import AccessLevel, Module
from app.moysklad.models import SourceLink, SourceRecord, SupplierInvoiceStatus
from app.moysklad.supplier_invoices import BUILT_IN


def source(db, kind, key, **payload):
    row = SourceRecord(entity_type=kind, source_id=key, title=key, search_text=key, payload=payload)
    db.add(row)
    db.flush()
    return row


def test_supply_invoice_columns_and_configurable_audited_statuses(db, api, make_user):
    owner = make_user(admin=True)
    client = api(owner)
    for code, label in BUILT_IN.items():
        db.add(SupplierInvoiceStatus(code=code, label=label, label_key=label.casefold()))
    source(db, 'store', 'Склад Технология')
    source(db, 'counterparty', 'Поставщик')
    source(db, 'currency', 'rub', isoCode='RUB')
    db.add(SourceLink(source_key='counterparty/Поставщик', target_type='counterparties', target_id=77))
    unpaid = source(db, 'invoicein', 'invoice-1', name='42', moment='2026-11-19 11:00:00',
        agent={'id': 'Поставщик'}, store={'id': 'Склад Технология'}, rate={'currency': {'id': 'rub'}},
        sum=25000, payedSum=10000, shippedSum=22500, applicable=True, description='По договору',
        incomingNumber='Вх-42', incomingDate='2026-11-19 10:00:00')
    paid = source(db, 'invoicein', 'invoice-2', sum=10000, payedSum=10000, applicable=True)
    waiting = source(db, 'invoicein', 'invoice-3', sum=18000, payedSum=0, paymentPlannedMoment='2099-12-31 12:00:00')
    db.commit()
    listing = '/api/moysklad/records?entity_type=invoicein'
    def read():
        response = client.get(listing)
        assert response.status_code == 200, response.text
        return {row['id']: row['invoice'] for row in response.json()['items']}

    values = read()
    invoice = values[unpaid.id]
    assert (invoice['number'], invoice['agent_name'], invoice['warehouse']) == ('42', 'Поставщик', 'Склад Технология')
    assert (invoice['amount'], invoice['paid'], invoice['accepted'], invoice['currency']) == ('250.00', '100.00', '225.00', 'RUB')
    assert invoice['status_label'] == 'Не оплачено' and invoice['counterparty_id'] == 77
    assert invoice['incoming_number'] == 'Вх-42' and invoice['comment'] == 'По договору'
    assert values[paid.id]['status_label'] == 'Оплачено'
    assert values[waiting.id]['status_label'] == 'Ожидаем' and values[waiting.id]['planned_payment_date'] == '2099-12-31'

    settings = '/api/moysklad/supplier-invoice-statuses'
    created = client.post(settings, json={'label': 'На сверке'}).json()
    assert client.post(settings, json={'label': '  на СВЕРКЕ '}).status_code == 409
    path = f'/api/moysklad/supplier-invoices/{unpaid.id}/status'
    response = client.patch(path, json={'status_id': created['id'], 'version': 0})
    assert response.status_code == 200 and response.json()['status_label'] == 'На сверке'
    assert client.patch(path, json={'status_id': None, 'version': 0}).status_code == 409
    renamed = client.patch(f'{settings}/{created["id"]}', json={'label': 'На проверке', 'version': 1})
    assert renamed.status_code == 200 and read()[unpaid.id]['status_label'] == 'На проверке'
    assert client.patch(f'/api/moysklad/supplier-invoices/{paid.id}/status',
                        json={'status_id': created['id'], 'version': 0}).status_code == 409
    paid_def = db.query(SupplierInvoiceStatus).filter_by(code='paid').one()
    assert client.patch(path, json={'status_id': paid_def.id, 'version': 1}).status_code == 422
    assert client.patch(f'{settings}/{paid_def.id}', json={'label': 'Оплата подтверждена', 'version': 1}).status_code == 200
    assert read()[paid.id]['status_label'] == 'Оплата подтверждена'
    assert client.patch(path, json={'status_id': None, 'version': 1}).json()['status_label'] == 'Не оплачено'
    assert db.get(SourceRecord, unpaid.id).payload['payedSum'] == 10000

    client = api(make_user(Module.ACCOUNTING, level=AccessLevel.VIEW))
    assert not client.get(listing).json()['can_edit_invoice_statuses']
    assert client.post(settings, json={'label': 'Другой'}).status_code == 403
    assert client.patch(path, json={'status_id': None, 'version': 2}).status_code == 403
