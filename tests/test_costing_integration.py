"""Exercise the native recipe/quote/order/stock chain on disposable data."""
from copy import deepcopy
from decimal import Decimal
from uuid import uuid4

import pytest

from app.accounting.models import Counterparty, Organization
from app.common.module_access import Module
from app.costing.models import CostingItem, CostingMaterial, CostingOrderLink
from app.crm.models import CRMEntity
from app.moysklad.recipes import Recipe, ManufactureRun
from app.sales.models import SalesOrder
from app.warehouse.models import WarehouseMaterial, Warehouse, MaterialCategory, StockMovement
from tests.test_costing import EXPENSES, PREFIX, request
from tests.costing_scenarios import ApprovalScenario


@pytest.fixture
def native(db, api, make_user):
    owner = make_user(admin=True)
    approvals=ApprovalScenario(db,api,make_user,owner)
    client = api(owner)
    client.approvals=approvals
    raw = WarehouseMaterial(title='Доска интеграции', code='COST-RAW', warehouse=Warehouse.TECHNOLOGY,
        category=MaterialCategory.NONE, unit='м3', is_fractional=True, quantity_in_stock=100, purchase_price=50, threshold=0)
    product = WarehouseMaterial(title='Изделие интеграции', code='COST-PRODUCT', warehouse=Warehouse.TECHNOLOGY,
        category=MaterialCategory.NONE, unit='шт', is_fractional=False, quantity_in_stock=0, purchase_price=0, threshold=0)
    buyer = Counterparty(name='Покупатель интеграции', is_active=True)
    deal = CRMEntity(kind='deal', name='Сделка интеграции', payload={})
    db.add_all([raw, product, buyer, deal]); db.flush()
    recipe = Recipe(title='Общая техкарта', version=1, archived=False, content={'inputs': [
        {'material_id':raw.id, 'title':raw.title, 'unit':raw.unit, 'quantity':'4'}], 'outputs': [
        {'material_id':product.id, 'title':product.title, 'unit':product.unit, 'quantity':'2'}], 'stages':[], 'warnings':[]})
    db.add(recipe); db.commit()
    return client, owner, raw, product, buyer, db.query(Organization).first(), deal, recipe


def imported(native, db):
    client, _, _, _, _, _, _, recipe = native
    response = client.post(f'{PREFIX}/integration/recipes/{recipe.id}/import', json={'warehouse':Warehouse.TECHNOLOGY.value})
    assert response.status_code == 200, response.text
    row = db.get(CostingItem, response.json()['item_id'])
    data = deepcopy(row.content); data.update(expenses=EXPENSES, retail_price='300')
    response = client.put(f'{PREFIX}/items/{row.id}', json={'version':row.version, 'kind':'template', 'title':row.title, 'content':data})
    assert response.status_code == 200, response.text
    return response.json()


def approved(native, db, **extra):
    client, _, _, _, _, _, deal, _ = native
    template = imported(native, db)
    estimate=client.approvals.priced(request(template,quantity='2',crm_deal_id=deal.id,**extra))
    return client.approvals.approve(estimate)


def new_order(native, estimate):
    client, _, _, _, buyer, org, _, _ = native
    body = {'version':estimate['version'],'request_id':str(uuid4()),'reason':'Интеграционная проверка','counterparty_id':buyer.id,'organization_id':org.id}
    response = client.post(f'{PREFIX}/integration/estimates/{estimate["id"]}/create-order', json=body)
    assert response.status_code == 201, response.text
    return response.json(), body


def test_import_uses_existing_stock_and_price_without_duplicate_or_movement(native, db):
    client, _, raw, product, _, _, _, recipe = native
    original = deepcopy(recipe.content)
    template = imported(native, db)
    mat = db.query(CostingMaterial).one()
    assert mat.warehouse_material_id == raw.id and mat.price is None
    assert Decimal(template['content']['lines'][0]['quantity']) == 2
    assert template['content']['warehouse_product_id'] == product.id
    again = client.post(f'{PREFIX}/integration/recipes/{recipe.id}/import', json={'warehouse':Warehouse.TECHNOLOGY.value})
    assert again.json()['item_id'] == template['id'] and not again.json()['created']
    assert db.query(CostingMaterial).count() == db.query(CostingItem).count() == 1
    assert db.query(StockMovement).count() == 0 and raw.quantity_in_stock == 100
    assert db.get(Recipe, recipe.id).content == original
    raw.purchase_price = 70; db.commit()
    materials = client.get(PREFIX+'/materials').json()
    assert Decimal(materials[0]['price']) == 70


def test_approved_quote_creates_native_order_once_and_links_deal(native, db):
    client, _, _, product, buyer, _, deal, _ = native
    estimate = approved(native, db)
    link, body = new_order(native, estimate)
    again = client.post(f'{PREFIX}/integration/estimates/{estimate["id"]}/create-order', json=body)
    assert again.json()['order_id'] == link['order_id']
    assert db.query(SalesOrder).count() == db.query(CostingOrderLink).count() == 1
    order = db.get(SalesOrder, link['order_id'])
    assert order.status == 'draft' and order.counterparty_id == buyer.id
    assert order.lines[0]['material_id'] == product.id and Decimal(order.lines[0]['quantity']) == 2
    assert Decimal(order.lines[0]['line_total']) == Decimal(estimate['price'])
    context = client.get(PREFIX+'/context', params={'kind':'deal','id':deal.id}).json()
    assert context['estimates'][0]['id'] == estimate['id']
    assert client.get(f'{PREFIX}/estimates/{estimate["id"]}').json()['context']['sales_order_id'] == order.id
    other = Counterparty(name='Другой покупатель', is_active=True); db.add(other); db.commit()
    assert client.post(f'{PREFIX}/integration/estimates/{estimate["id"]}/create-order', json={**body,'counterparty_id':other.id}).status_code == 409
    assert db.query(SalesOrder).count() == 1 and db.query(StockMovement).count() == 0


def test_changed_order_line_requires_new_approved_match(native, db):
    client, _, _, product, _, _, _, _ = native
    estimate = approved(native, db); link, _ = new_order(native, estimate)
    oid = link['order_id']
    changed = client.patch(f'/api/sales/orders/{oid}', json={'version':1,'lines':[{'material_id':product.id,'quantity':'2','unit_price':'400'}]})
    assert changed.status_code == 200, changed.text
    assert not client.get(PREFIX+'/context',params={'kind':'order','id':oid}).json()['links'][0]['fresh']
    assert client.post(f'/api/sales/orders/{oid}/confirm',json={'version':changed.json()['version']}).status_code == 409
    assert client.post(f'{PREFIX}/integration/estimates/{estimate["id"]}/bind-order',json={'version':estimate['version'],'request_id':str(uuid4()),'reason':'Интеграционная проверка','order_id':oid,'order_version':2,'line_index':0}).status_code == 422


def test_production_uses_pinned_bom_and_normal_atomic_stock_flow(native, db):
    client, _, raw, product, _, _, _, master = native
    original = deepcopy(master.content)
    estimate = approved(native, db); link, _ = new_order(native, estimate)
    body = {'version':estimate['version'],'request_id':str(uuid4()),'reason':'Интеграционная проверка','order_id':link['order_id'],'line_index':0}
    response = client.post(f'{PREFIX}/integration/estimates/{estimate["id"]}/production-recipe', json=body)
    assert response.status_code == 200, response.text
    rid = response.json()['recipe_id']
    assert client.post(f'{PREFIX}/integration/estimates/{estimate["id"]}/production-recipe', json=body).json()['recipe_id'] == rid
    assert db.query(Recipe).count() == 2 and db.query(StockMovement).count() == 0
    recipe = db.get(Recipe, rid)
    assert Decimal(recipe.content['inputs'][0]['quantity']) == 4
    assert recipe.content['inputs'][0]['material_id'] == raw.id
    manufacturing = {'warehouse':Warehouse.TECHNOLOGY.value,'multiplier':'0.5','request_id':str(uuid4())}
    assert client.post(f'/api/moysklad/techcards/{rid}/manufacture', json=manufacturing).status_code == 409
    assert db.query(ManufactureRun).count() == 0
    assert client.post(f'/api/sales/orders/{link["order_id"]}/confirm',json={'version':1}).status_code == 200
    response = client.post(f'/api/moysklad/techcards/{rid}/manufacture', json=manufacturing)
    assert response.status_code == 200, response.text
    assert client.post(f'/api/moysklad/techcards/{rid}/manufacture', json=manufacturing).json()['run_id'] == response.json()['run_id']
    db.expire_all()
    assert db.get(WarehouseMaterial, raw.id).quantity_in_stock == 98
    assert db.get(WarehouseMaterial, product.id).quantity_in_stock == 1
    changed = deepcopy(recipe.content); changed['inputs'][0]['quantity']='8'
    assert client.patch(f'/api/moysklad/techcards/{rid}',json={'version':1,'title':recipe.title,'content':changed}).status_code == 409
    assert client.patch(f'/api/sales/orders/{link["order_id"]}', json={'version':2,'lines':[{'material_id':product.id,'quantity':'3','unit_price':'300'}]}).status_code == 409
    second = client.post(f'/api/moysklad/techcards/{rid}/manufacture',json={**manufacturing,'request_id':str(uuid4())})
    assert second.status_code == 200, second.text
    assert client.post(f'/api/moysklad/techcards/{rid}/manufacture',json={**manufacturing,'request_id':str(uuid4())}).status_code == 409
    assert client.post(f'/api/sales/orders/{link["order_id"]}/cancel',json={'version':2,'request_id':str(uuid4()),'note':'Отмена'}).status_code == 409
    assert db.get(Recipe, master.id).content == original
    assert db.query(ManufactureRun).count() == 2 and db.query(StockMovement).count() == 4


def test_context_permissions_hide_costs_and_other_managers_quotes(native, db, api, make_user):
    _, _, _, _, _, _, deal, recipe = native
    estimate = approved(native, db)
    technician = api(make_user(Module.PRODUCTION))
    context = technician.get(PREFIX+'/context',params={'kind':'recipe','id':recipe.id})
    assert context.status_code == 200 and context.json()['estimates'] == []
    assert technician.get(PREFIX+'/context',params={'kind':'deal','id':deal.id}).status_code == 403
    assert technician.post(f'{PREFIX}/integration/recipes/{recipe.id}/import',json={'warehouse':Warehouse.TECHNOLOGY.value}).status_code == 403
    manager = make_user(Module.CLIENTS); manager.job_role='manager'; commercial = api(manager)
    context = commercial.get(PREFIX+'/context',params={'kind':'deal','id':deal.id})
    assert context.status_code == 200 and context.json()['estimates'] == []
    assert commercial.get(f'{PREFIX}/estimates/{estimate["id"]}').status_code == 404
    assert commercial.get(PREFIX+'/context',params={'kind':'recipe','id':recipe.id}).status_code == 403
