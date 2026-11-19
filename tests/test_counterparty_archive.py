from uuid import uuid4

from app.accounting.models import SupplierOrder, SupplierOrderStatus
from app.common.module_access import AccessLevel, Module
from app.warehouse.models import Supplier, SupplierStatus
from test_counterparty_requisites import P, create


def test_archive_from_card_preserves_documents_and_can_be_reversed(db, api, make_user):
    owner = make_user(admin=True)
    client = api(owner)
    row = create(client)
    path = f'{P}/{row["id"]}'
    supplier = db.get(Supplier, row['supplier_id'])
    order = SupplierOrder(
        supplier_id=supplier.id, number='З-42', items=[], total_cost=120,
        status=SupplierOrderStatus.ORDERED,
    )
    db.add(order)
    db.commit()

    archive = dict(request_id=str(uuid4()), etag=row['etag'], archived=True)
    read_only = api(make_user(Module.WAREHOUSE, level=AccessLevel.VIEW))
    assert read_only.post(path + '/archive', json=archive).status_code == 403
    client = api(owner)
    assert client.post(path + '/archive', json=archive).status_code == 409
    order.status = SupplierOrderStatus.RECEIVED
    db.commit()
    documents = client.get(path + '/documents').json()['items']
    assert any(doc['number'] == order.number for doc in documents)

    response = client.post(path + '/archive', json=archive)
    assert response.status_code == 200, response.text
    archived = response.json()
    assert archived['active'] is False
    assert archived['etag'] != row['etag']
    assert client.post(path + '/archive', json=archive).json() == archived
    assert client.get(P + '?archive=active').json()['items'] == []
    assert [item['id'] for item in client.get(P + '?archive=archived').json()['items']] == [row['id']]
    assert client.get(path + '/documents').json()['items'] == documents
    db.refresh(supplier)
    assert supplier.status == SupplierStatus.ARCHIVED
    assert client.post(path + '/archive', json={**archive, 'request_id': str(uuid4()), 'archived': False}).status_code == 409

    restore = dict(request_id=str(uuid4()), etag=archived['etag'], archived=False)
    response = client.post(path + '/archive', json=restore)
    assert response.status_code == 200 and response.json()['active'] is True
    db.refresh(supplier)
    assert supplier.status == SupplierStatus.ACTIVE
    history = client.get(path + '/history').json()
    assert [entry['kind'] for entry in history[:2]] == ['restore', 'archive']
