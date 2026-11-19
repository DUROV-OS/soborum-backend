from datetime import datetime, timezone
from uuid import uuid4

from app.accounting.models import Counterparty, CounterpartyKind
from app.clients.models import ContractSource
from app.clients.schemas import ClientCreate
from app.clients.service import create_client
from app.common.files import FilePurpose, save_bytes_file
from app.common.module_access import AccessLevel, Module
from app.counterparties.models import Contact, Details
from app.purchasing.control_models import PurchaseControlDocument
from app.warehouse.models import Supplier
from tests.test_counterparty_requisites import create, source

P = '/api/counterparties'


def patch(c, row, **values):
    return c.patch(f'{P}/{row["id"]}/registry', json={
        'etag': row['etag'], 'request_id': str(uuid4()), **values,
    })


def test_registry_filters_and_sort_full_result_before_paging(api, make_user, db):
    c = api(make_user(admin=True))
    supplier = Supplier(name='Ель', contacts=[{'kind': 'phone', 'value': '(473) 100-00-01'}])
    db.add(supplier)
    db.flush()
    first = Counterparty(name='Ель', inn='7707083893', supplier_id=supplier.id, comment='Бета')
    second = Counterparty(name='Ёлка', inn='7736050003', comment='Альфа доставка')
    third = Counterparty(name='Жук')
    db.add_all([third, second, first])
    db.flush()
    db.add(Details(counterparty_id=second.id, data={'kpp': '773601002'}))
    db.add_all([
        Contact(id=str(uuid4()), counterparty_id=second.id, data={'phone': '+7 900 222-00-10', 'primary': True}),
        Contact(id=str(uuid4()), counterparty_id=second.id, data={'phone': '+7 999 333-00-10'}),
    ])
    db.commit()

    def names(**params):
        r = c.get(P, params=params)
        assert r.status_code == 200, r.text
        return [item['card']['name'] for item in r.json()['items']]

    assert names() == ['Ель', 'Ёлка', 'Жук']
    assert names(name='ёЛ', inn='7736050003 / 773601002', phone='900-222', comment='ДОСТАВКА') == ['Ёлка']
    assert names(kpp='773601002') == ['Ёлка']
    assert names(phone='999333') == []
    assert names(phone='(473) 100') == ['Ель']
    assert names(status=['supplier', 'client']) == ['Ель']
    assert names(sort='inn', direction='desc', limit=1) == ['Ёлка']
    assert names(sort='phone', direction='desc', offset=1, limit=1) == ['Ель']
    assert names(sort='status', direction='desc') == ['Ель', 'Жук', 'Ёлка']
    assert names(sort='comment', direction='asc') == ['Ёлка', 'Ель', 'Жук']


def test_supplier_contract_and_comment_are_versioned_and_survive_sync(api, make_user, db, tmp_path, monkeypatch):
    from app.common.files import settings
    monkeypatch.setattr(settings, 'storage_dir', str(tmp_path))
    owner = make_user(admin=True)
    c = api(owner)
    row = create(c)
    remote = source(db, row['id'], description='Комментарий источника')
    preview = c.post(f'{P}/{row["id"]}/moysklad/preview').json()
    assert next(item for item in preview['changes'] if item['key'] == 'comment')['fill_empty']
    local = patch(c, row, comment='Локальная заметка').json()
    conflict = patch(c, row, comment='Вторая правка')
    assert conflict.status_code == 409
    assert conflict.json()['detail']['current']['comment'] == 'Локальная заметка'
    p = f'{P}/{row["id"]}'
    assert c.post(p+'/moysklad/apply', json={
        'request_id': str(uuid4()), 'preview_id': preview['id'], 'fields': ['comment'],
    }).status_code == 409
    stale = c.post(p+'/moysklad/preview').json()
    assert not next(item for item in stale['changes'] if item['key'] == 'comment')['fill_empty']
    assert patch(c, local, status='deal_closed').status_code == 422

    other = Supplier(name='Чужой поставщик')
    db.add(other)
    db.flush()
    file = save_bytes_file(db, 'Договор.pdf', 'application/pdf', b'synthetic contract', FilePurpose.COSTING_EVIDENCE, owner)
    link = PurchaseControlDocument(supplier_id=other.id, actor_id=owner.id, file_id=file.id)
    db.add(link)
    db.commit()
    details = {'status': 'deal_closed', 'evidence': {'file_id': file.id, 'locator': 'Лист 2'}, 'signed': True}
    assert patch(c, local, **details).status_code == 422
    link.supplier_id = row['supplier_id']
    db.commit()
    result = patch(c, local, **details)
    assert result.status_code == 200, result.text
    deal = result.json()
    assert deal['status'] == 'deal_closed' and deal['supplier_id'] == row['supplier_id']
    change = c.get(p+'/history').json()[0]
    assert c.get(p+f'/history/{change["id"]}/document').content == b'synthetic contract'
    remote.payload = {**remote.payload, 'phone': '+7 999 660 20 30'}
    db.commit()
    proposal = c.post(p+'/moysklad/preview').json()
    fields = [item['key'] for item in proposal['changes'] if item['fill_empty']]
    applied = c.post(p+'/moysklad/apply', json={'request_id': str(uuid4()), 'preview_id': proposal['id'], 'fields': fields})
    assert applied.status_code == 200, applied.text
    after = applied.json()
    assert after['status'] == 'deal_closed' and after['comment'] == 'Локальная заметка'
    assert after['phone'] == '+7 999 660 20 30'
    assert c.get(P, params={'status': 'deal_closed'}).json()['total'] == 1
    view = api(make_user(Module.WAREHOUSE, level=AccessLevel.VIEW))
    assert patch(view, after, comment='Нет прав').status_code == 403
    assert view.get(p+f'/history/{change["id"]}/document').status_code == 403


def test_client_profile_binding_and_verified_contract(api, make_user, db, tmp_path, monkeypatch):
    from app.common.files import settings
    monkeypatch.setattr(settings, 'storage_dir', str(tmp_path))
    owner = make_user(admin=True)
    c = api(owner)
    client = create_client(db, ClientCreate(full_name='Клиент', phone='+79990000000', email='test@example.org'))
    cp = Counterparty(name='Контрагент')
    db.add(cp)
    db.commit()
    path = f'{P}/{cp.id}'
    assert c.get(P+'/registry/profiles', params={'role': 'client'}).json()[0]['id'] == client.id
    row = c.get(path).json()
    linked = patch(c, row, status='client', profile_id=client.id)
    assert linked.status_code == 200, linked.text
    assert c.get(P+'/registry/profiles', params={'role': 'client'}).json() == []
    row = linked.json()
    assert row['phone'] == '+79990000000'
    assert c.get(P, params={'phone': '999 000'}).json()['items'][0]['id'] == row['id']
    accountant = api(make_user(Module.ACCOUNTING))
    assert accountant.get(path).json()['phone'] == ''
    assert accountant.get(P, params={'phone': '999 000'}).json()['items'] == []
    c = api(owner)
    assert patch(c, row, status='deal_closed').status_code == 422
    file = save_bytes_file(db, 'Договор.pdf', 'application/pdf', b'confirmed', FilePurpose.CONTRACT, owner)
    client.contract_file_id = file.id
    client.contract_source = ContractSource.UPLOADED
    client.contract_verified_at = datetime.now(timezone.utc)
    client.contract_verified_by_id = owner.id
    db.commit()
    result = patch(c, row, status='deal_closed')
    assert result.status_code == 200, result.text
    assert result.json()['status'] == 'deal_closed'
    assert db.get(Counterparty, cp.id).kind == CounterpartyKind.CLIENT
