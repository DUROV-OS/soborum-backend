from types import SimpleNamespace
from decimal import Decimal
from app.common.module_access import Module, AccessLevel
from app.production.library_service import build_card
from app.production.library_data import MODEL_BY_KEY
from app.moysklad.recipes import Recipe


def recipe(id,title,inputs,outputs=None,stages=None):
    return SimpleNamespace(id=id,title=title,source_id=f'source-{id}',version=1,archived=False,
        content={'inputs':inputs,'outputs':outputs or [{'source_id':f'product-{id}','quantity':'1'}],
                 'stages':stages or [],'warnings':[]})


def line(id,qty,unit='шт'):
    return {'source_id':id,'title':id,'unit':unit,'quantity':qty}


def test_exact_expansion_uses_output_quantity_and_combines_material_once():
    root=recipe(1,'DH-64 Барн',[line('kit','2'),line('wood','1')])
    child=recipe(2,'DH-64 Комплект каркаса',[line('wood','6')],[line('kit','2')])
    data=build_card(MODEL_BY_KEY['barn-dh64'],[root,child])
    assert len(data['materials'])==1
    assert Decimal(data['materials'][0]['quantity'])==7
    assert len(data['materials'][0]['sources'])==2
    assert data['basis']=='exact' and not data['permitted_for_manufacture']


def test_analogue_does_not_invent_quantities_or_scale_area():
    root=recipe(1,'DH-64 Барн',[line('wood','125')])
    data=build_card(MODEL_BY_KEY['barn-dh156'],[root])
    assert data['basis']=='analogue'
    assert data['materials'][0]['quantity'] is None
    assert Decimal(data['materials'][0]['basis_quantity'])==125
    assert 'cost' not in str(data)


def test_same_number_other_series_is_not_exact():
    root=recipe(1,'DH27 Флет Домокомплект',[line('wood','2')])
    data=build_card(MODEL_BY_KEY['barn-dh27'],[root])
    assert data['basis']=='missing' and data['materials']==[]


def test_prices_and_raw_stage_metadata_are_not_returned():
    root=recipe(1,'DH-64 Барн',[line('wood','2')],stages=[{
        'name':'Каркас','standardHour':2,'labourCost':777,'standardHourCost':888,
        'cost':999,'customer':{'phone':'secret-phone'}}])
    data=build_card(MODEL_BY_KEY['barn-dh64'],[root])
    assert data['operations'][0]['hours']=='2'
    assert not any(x in str(data) for x in ['labourCost','standardHourCost','secret-phone','999'])


def test_cyclic_nested_recipe_stays_unresolved():
    root=recipe(1,'DH-64 Барн',[line('b','1')],[line('a','1')])
    child=recipe(2,'Комплект',[line('a','1')],[line('b','1')])
    data=build_card(MODEL_BY_KEY['barn-dh64'],[root,child])
    assert data['materials'][0]['quantity'] is None
    assert any('Не раскрыт' in w for w in data['warnings'])


def seed_recipe(db):
    row=Recipe(title='DH-64 Барн',source_id='fixture',content={'inputs':[line('wood','2')],
        'outputs':[line('home','1')],'stages':[],'warnings':[]})
    db.add(row);db.commit();return row


def test_self_reference_invalid_root_and_excessive_nested_quantity_are_unresolved():
    root=recipe(1,'DH-64 Барн',[line('a','1')],[line('a','1')])
    data=build_card(MODEL_BY_KEY['barn-dh64'],[root])
    assert data['materials'][0]['quantity'] is None
    root=recipe(1,'DH-64 Барн',[line('wood','2')],[line('a','0')])
    assert build_card(MODEL_BY_KEY['barn-dh64'],[root])['materials'][0]['quantity'] is None
    root=recipe(1,'DH-64 Барн',[line('kit','10000000')])
    child=recipe(2,'Комплект',[line('wood','10000000')],[line('kit','1')])
    assert build_card(MODEL_BY_KEY['barn-dh64'],[root,child])['materials'][0]['quantity'] is None


def test_roles_generate_idempotence_and_version_guard(db,api,make_user):
    seed_recipe(db)
    sales=api(make_user(Module.CLIENTS))
    assert sales.get('/api/production/library/models').status_code==403
    assert sales.get('/api/production/library/standards').status_code==403
    view=api(make_user(Module.PRODUCTION,level=AccessLevel.VIEW))
    assert view.post('/api/production/library/generate').status_code==403
    owner=api(make_user(admin=True))
    assert owner.post('/api/production/library/generate').json()['created']==14
    assert owner.post('/api/production/library/generate').json()['unchanged']==14
    assert db.query(Recipe).count()==1
    detail=owner.get('/api/production/library/models/barn-dh64').json()
    assert detail['content']['materials'][0]['quantity']=='2'
    payload={'version':detail['version'],'quantities':[{'key':detail['content']['materials'][0]['key'],'quantity':'3'}],'notes':'Проверить с КР'}
    assert owner.patch('/api/production/library/models/barn-dh64',json=payload).status_code==200
    assert owner.patch('/api/production/library/models/barn-dh64',json=payload).status_code==409
    assert owner.post('/api/production/library/generate').json()['created']==0
    assert owner.get('/api/production/library/models/barn-dh64').json()['content']['materials'][0]['quantity']=='3'
    assert not owner.get('/api/production/library/models/barn-dh64').json()['content']['permitted_for_manufacture']


def test_project_links_are_private_until_owner_releases(db,api,make_user):
    admin=make_user(admin=True);worker=make_user(Module.PRODUCTION)
    owner=api(admin);owner.post('/api/production/library/generate')
    url='/api/production/library/models/barn-dh64/project-link'
    payload={'version':1,'kind':'AR','url':'https://example.com/private','revision':'1'}
    assert owner.put(url,json=payload).status_code==200
    production=api(worker)
    assert production.get('/api/production/library/models/barn-dh64').json()['content']['project_links']['AR'] is None
    assert production.put(url,json={**payload,'version':2}).status_code==403
    owner=api(admin)
    assert owner.put(url,json={**payload,'version':2,'production_safe':True}).status_code==200
    production=api(worker)
    assert production.get('/api/production/library/models/barn-dh64').json()['content']['project_links']['AR']['production_safe']


def test_export_and_invalid_quantity(db,api,make_user):
    row=seed_recipe(db)
    row.content={**row.content,'inputs':[{**line('wood','2'),'title':'=HYPERLINK("example")'}]};db.commit()
    client=api(make_user(admin=True));client.post('/api/production/library/generate')
    data=client.get('/api/production/library/models/barn-dh64').json()
    key=data['content']['materials'][0]['key']
    assert client.patch('/api/production/library/models/barn-dh64',json={'version':1,'quantities':[{'key':key,'quantity':-1}]}).status_code==422
    exported=client.get('/api/production/library/models/barn-dh64/export')
    assert exported.status_code==200 and "'=HYPERLINK" in exported.text
    assert 'Цена' not in exported.text and 'Телефон' not in exported.text
