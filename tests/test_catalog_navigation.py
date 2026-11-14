from copy import deepcopy
from app.common.module_access import Module
from app.costing.models import CostingItem
from app.house_models.models import HouseModelCard, HouseModelKind, HouseModelConfirmation
from app.moysklad.recipes import Recipe
from tests.test_costing import material, item


def seed_catalog(db, api, make_user):
    owner=make_user(admin=True)
    client=api(owner)
    mat=material(client)
    template=item(client,mat)
    model=HouseModelCard(key='barn-dh64',title='Барн DH 64',kind=HouseModelKind.CATALOG,series='barn',
        confirmation=HouseModelConfirmation.PARTIAL,confirmation_label='Есть сведения',source_note_path='internal/source',
        area_footprint_m2=64.57,area_total_m2=56.33,price_site_rub=1234567,client_name='SECRET_CLIENT',
        deal_amount_rub=999999,economics_md='SECRET_COST',files_md='SECRET_FILES',notes_md='SECRET_NOTES',
        characteristics_md='Высота: 3 м',planning_md='Две комнаты',modules_md='Два модуля')
    recipe=Recipe(title='DH-64 Барн',source_id='catalog-fixture',content={'inputs':[
        {'title':'Доска','source_id':'wood','unit':'м3','quantity':'2'}],
        'outputs':[{'source_id':'house','quantity':'1'}],'stages':[]})
    db.add_all([model,recipe]);db.flush()
    row=db.get(CostingItem,template['id'])
    row.content={**row.content,'house_model_key':model.key,'source_recipe_id':recipe.id,'source_recipe_version':recipe.version,'note':'SECRET_ESTIMATE_NOTE'}
    db.commit()
    assert client.post('/api/production/library/generate').status_code==200
    return owner,row,recipe


def test_owner_catalog_links_point_to_specific_records_without_mutations(db,api,make_user):
    owner,row,recipe=seed_catalog(db,api,make_user)
    client=api(owner)
    before=deepcopy(row.content)
    data=client.get(f'/api/costing/catalog/items/{row.id}').json()
    assert data['price']==client.get('/api/costing/catalog').json()[0]['price']
    assert data['calculation']['lines'][0]['name']=='Доска'
    hrefs={l['href'] for l in data['connections']}
    assert '/house-models/barn-dh64' in hrefs
    assert '/production-library?tab=cards&model=barn-dh64' in hrefs
    assert f'/recipes?recipe={recipe.id}' in hrefs
    assert client.get('/api/costing/catalog/models/barn-dh64').json()['items'][0]['id']==row.id
    assert len(client.get('/api/costing/catalog/models').json()['items'])==14
    assert row.content==before and row.version==1


def test_sales_detail_allowlist_does_not_open_internal_routes_or_pii(db,api,make_user):
    _,row,_=seed_catalog(db,api,make_user)
    manager=make_user(Module.CLIENTS,Module.COSTING,Module.PRODUCTION,Module.HOUSE_MODELS)
    manager.job_role='manager';db.commit()
    client=api(manager)
    item_data=client.get(f'/api/costing/catalog/items/{row.id}').json()
    assert item_data['internal'] is False
    assert 'calculation' not in item_data and 'note' not in item_data
    assert all(not l['href'].startswith(('/recipes','/production-library','/house-models')) for l in item_data['connections'])
    response=client.get('/api/costing/catalog/models/barn-dh64')
    assert response.status_code==200 and 'Высота' in response.text
    assert 'SECRET' not in response.text and 'deal_amount' not in response.text
    assert client.get('/api/costing/catalog/models/individual-client').status_code==404


def test_production_reverse_links_never_add_commercial_access(db,api,make_user):
    _,row,_=seed_catalog(db,api,make_user)
    client=api(make_user(Module.PRODUCTION))
    links=client.get('/api/production/library/models/barn-dh64/connections')
    assert links.status_code==200
    assert not any(l['href'].startswith(('/costing','/house-models','/recipes')) for l in links.json()['connections'])
    for path in ('catalog/models','catalog/models/barn-dh64',f'catalog/items/{row.id}','catalog/model-connections/barn-dh64'):
        assert client.get('/api/costing/'+path).status_code==403
    client=api(make_user(Module.CLIENTS))
    assert client.get('/api/production/library/models/barn-dh64/connections').status_code==403


def test_unlinked_or_archived_item_does_not_guess_a_house(db,api,make_user):
    owner,row,_=seed_catalog(db,api,make_user)
    row.content={**row.content,'house_model_key':None,'source_recipe_id':None}
    row.title='Barn DH 64 almost the same title';db.commit()
    data=api(owner).get(f'/api/costing/catalog/items/{row.id}').json()
    assert data['model_linked'] is False
    assert all('model=' not in l['href'] and '/house-models/' not in l['href'] for l in data['connections'])
    row.archived=True;db.commit()
    assert api(make_user(Module.CLIENTS)).get(f'/api/costing/catalog/items/{row.id}').status_code==404
    assert api(owner).get(f'/api/costing/catalog/items/{row.id}').status_code==200
    assert api(owner).get('/api/costing/catalog/items/999999').status_code==404
