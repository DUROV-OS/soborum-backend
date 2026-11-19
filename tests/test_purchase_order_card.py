from uuid import uuid4

from app.accounting.models import SupplierOrder
from app.common.files import settings
from app.common.module_access import AccessLevel, Module
from app.moysklad.models import SourceRecord
from app.warehouse.reservation_models import StockReservation
from test_purchasing_workflows import setup


def test_order_position_stock_and_source_vat(db, api, make_user):
    client, owner, _, material, order = setup(db, api, make_user)
    row = db.get(SupplierOrder, order['id'])
    row.source_id = 'order-source'
    db.add(StockReservation(material_id=material.id, quantity=3, unit=material.unit, purpose='Проверка остатка',
                            actor_id=owner.id, request_id=str(uuid4()), parameters={}))
    db.add(SourceRecord(entity_type='purchaseorder', source_id='order-source', title='З-1', search_text='',
        payload={'vatEnabled': True, 'vatIncluded': True, 'positions': {'rows': [
            {'assortment': {'name': 'Брус'}, 'quantity': 10, 'price': 2000, 'vat': 20},
        ]}}))
    db.commit()
    path = f'/api/purchasing/orders/{order["id"]}/positions'
    response = client.get(path)
    assert response.status_code == 200, response.text
    line = response.json()[0]
    assert {'stock': 10, 'available': 7} == {key: float(line[key]) for key in ['stock', 'available']}
    assert (line['vat_rate'], line['amount_with_vat'], line['amount']) == ('20', '200.00', '166.67')
    row.items = [{**row.items[0], 'unit_price': 21}]
    db.commit()
    line = client.get(path).json()[0]
    assert line['vat_rate'] is None and line['amount_with_vat'] is None and line['amount'] == '210.00'


def test_order_attachment_upload_retry_scope_and_archive(db, api, make_user, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, 'storage_dir', str(tmp_path))
    client, owner, supplier, _, order = setup(db, api, make_user)
    other = SupplierOrder(supplier_id=supplier.id, items=[], total_cost=1)
    db.add(other)
    db.commit()
    order_id = order['id']
    path = f'/api/purchasing/orders/{order_id}/files'
    request = {'request_id': str(uuid4())}
    content = b'%PDF-1.7\nsynthetic attachment'
    upload = {'file': ('Счёт.pdf', content, 'application/pdf')}
    assert client.get(path).json() == []
    assert client.post(path, data=request, files={'file': ('bad.pdf', b'bad', 'application/pdf')}).status_code == 422
    first = client.post(path, data=request, files=upload)
    assert first.status_code == 201, first.text
    data = first.json()
    assert client.post(path, data=request, files=upload).json() == data
    assert client.get(path).json() == [data]
    asset_path = '/api/purchasing' + data['path']
    assert client.get(asset_path).content == content
    assert client.get(asset_path.replace(f'/orders/{order_id}/', f'/orders/{other.id}/')).status_code == 404
    assert client.delete(f'/api/accounting/supplier-orders/{order_id}').status_code == 409
    view = api(make_user(Module.ACCOUNTING, level=AccessLevel.VIEW))
    assert view.get(asset_path).content == content
    assert view.post(path, data=request, files=upload).status_code == 403
    assert api(make_user()).get(asset_path).status_code == 403
    client = api(owner)
    row = db.get(SupplierOrder, order_id)
    row.archived = True
    db.commit()
    assert client.get(asset_path).content == content
    assert client.post(path, data={'request_id': str(uuid4())}, files=upload).status_code == 409


def test_order_lists_and_downloads_only_its_imported_file(db, api, make_user, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, 'storage_dir', str(tmp_path))
    client, _, _, _, order = setup(db, api, make_user)
    db.get(SupplierOrder, order['id']).source_id = 'source-file-order'
    directory = tmp_path / 'moysklad' / 'files'
    directory.mkdir(parents=True)
    content = b'%PDF-1.7\nimported test'
    (directory / 'imported.pdf').write_bytes(content)
    db.add(SourceRecord(entity_type='purchaseorder', source_id='source-file-order', title='З-1', search_text='',
        payload={'_downloaded_files': [{'local_name': 'imported.pdf', 'filename': 'Поставка.pdf',
                                       'size': len(content), 'content_type': 'application/pdf'}]}))
    db.commit()
    result = client.get(f'/api/purchasing/orders/{order["id"]}/files').json()
    assert len(result) == 1 and result[0]['size'] == len(content) and result[0]['source'] == 'МойСклад'
    assert client.get('/api/purchasing' + result[0]['path']).content == content
