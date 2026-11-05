"""Read-only navigation between commercial catalog, house models and technical drafts.

Links follow explicit stored relations and existing permissions. No fuzzy matching,
source writes, price recalculation persistence or customer-document access.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.common.module_access import Module
from app.core.deps import get_current_user
from app.db.session import get_db
from app.costing import service as s
from app.costing.models import CostingItem
from app.house_models.models import HouseModelCard, HouseModelKind
from app.moysklad.recipes import Recipe
from app.production.library_data import MODEL_BY_KEY, SITE_MODELS
from app.production.library_models import HouseTechnicalCard

router = APIRouter()


def commercial(user):
    return s.cost_access(user) or user.has_access(Module.CLIENTS)


def technical(user):
    return user.job_role != 'manager' and user.has_access(Module.PRODUCTION)


def link(title, href, description='', external=False):
    return {'title': title, 'href': href, 'description': description, 'external': external}


def model_connections(db, user, key):
    model = db.query(HouseModelCard).filter_by(key=key).first()
    draft = db.query(HouseTechnicalCard).filter_by(model_key=key).first()
    result = []
    if commercial(user) and key in MODEL_BY_KEY:
        result.append(link('Карточка дома в каталоге', '/costing/models/'+key, 'Площади, комплектации и связанные цены'))
    if model and s.cost_access(user) and user.has_access(Module.HOUSE_MODELS):
        result.append(link('Полная карточка дома', '/house-models/'+key, 'Проект, планировка, история и внутренние сведения'))
    if draft and technical(user):
        result += [link('Техкарта дома', '/production-library?tab=cards&model='+key,
                        'Черновик · версия '+str(draft.version)+' · '+str(len(draft.content.get('materials',[])))+' материалов'),
                   link('Проекты АР и КР', '/production-library?tab=projects&model='+key, 'Документы и редакции этой модели'),
                   link('Нормативные документы', '/production-library?tab=standards&model='+key, 'ГОСТы и СП для проверки применимости')]
    if commercial(user):
        for item in db.query(CostingItem).filter_by(archived=False).order_by(CostingItem.id):
            if item.content.get('house_model_key') == key:
                result.append(link(item.title, '/costing/catalog/'+str(item.id), 'Связанная прайсовая комплектация'))
    if key in MODEL_BY_KEY:
        result.append(link('Дом на сайте компании', MODEL_BY_KEY[key]['site_url'], 'Публичная страница модели', True))
    return result


def item_connections(db, user, item):
    data = item['content']
    result = []
    if s.cost_access(user):
        result.append(link('Состав и расчёт', f'/costing?tab={item["kind"]}&item={item["id"]}&preview=1',
                           'Материалы, себестоимость, прайсовая цена и маржа'))
    key = data.get('house_model_key')
    if key:
        result += model_connections(db, user, key)
    recipe = db.get(Recipe, data['source_recipe_id']) if data.get('source_recipe_id') else None
    if recipe and not recipe.archived and s.cost_access(user) and user.job_role != 'manager' and (user.has_access(Module.PRODUCTION) or user.has_access(Module.WAREHOUSE)):
        result.append(link('Исходная техкарта', '/recipes?recipe='+str(recipe.id),
                           f'Версия источника {data.get("source_recipe_version") or "не указана"} · текущая версия {recipe.version}'))
    return result


def catalog_brief(item, source):
    calc = s.inspect_item(item, source)
    data = item['content']
    area = s.geometry(data)
    return {'id': item['id'], 'title': item['title'], 'kind': item['kind'], 'version': item['version'],
            'warehouse_product_id': data.get('warehouse_product_id'), 'house_model_key': data.get('house_model_key'),
            **{k: data[k] for k in ('code','category','model','unit','mode','image')},
            'area': str(area) if area else None, 'price': calc['price'],
            'available': calc['price'] is not None and not calc['missing']}


def read(user=Depends(get_current_user)):
    s.can_estimate(user)
    return user


@router.get('/catalog/items/{id}')
def item_detail(id: int, db: Session=Depends(get_db), user=Depends(read)):
    source = s.sources(db)
    item = source['items'].get(str(id))
    if not item or (item['archived'] and not s.cost_access(user)):
        s.fail('Позиция каталога не найдена',404)
    data = item['content']
    calc = s.inspect_item(item,source)
    packages = []
    for entry in data.get('addons',[]):
        addon = source['items'].get(str(entry['addon_id']))
        if addon and not addon['archived']:
            packages.append({**catalog_brief(addon,source),'quantity':entry['quantity']})
    return {**catalog_brief(item,source), 'archived':item['archived'],
            'dimensions':{k:data.get(k) for k in ('length','width','modules')},
            'price_basis':'За м²' if data['mode']=='area' else 'За изделие',
            'retail_rate':data.get('retail_rate') if data['mode']=='area' else None,
            'packages':packages, 'connections':item_connections(db,user,item),
            'model_linked':bool(data.get('house_model_key')),
            'internal':s.cost_access(user),
            **({'calculation':calc,'note':data.get('note','')} if s.cost_access(user) else {})}


def model_brief(db, model, definition):
    draft = db.query(HouseTechnicalCard).filter_by(model_key=definition['key']).first()
    linked = db.query(CostingItem).filter_by(archived=False).all()
    return {**definition,
            'area_footprint_m2':model.area_footprint_m2 if model else None,
            'area_total_m2':model.area_total_m2 if model else None,
            'price_site_rub':model.price_site_rub if model else None,
            'template_count':sum(i.kind=='template' and i.content.get('house_model_key')==definition['key'] for i in linked),
            'technical_status':draft.content.get('basis') if draft else 'missing',
            'material_count':len(draft.content.get('materials',[])) if draft else 0}


@router.get('/catalog/models')
def models(db: Session=Depends(get_db), user=Depends(read)):
    found={m.key:m for m in db.query(HouseModelCard).filter_by(kind=HouseModelKind.CATALOG)}
    return {'items':[model_brief(db,found.get(d['key']),d) for d in SITE_MODELS]}


@router.get('/catalog/models/{key}')
def model_detail(key: str, db: Session=Depends(get_db), user=Depends(read)):
    definition=MODEL_BY_KEY.get(key)
    if definition is None:
        s.fail('Каталожная модель не найдена',404)
    model=db.query(HouseModelCard).filter_by(key=key,kind=HouseModelKind.CATALOG).first()
    source=s.sources(db)
    items=[catalog_brief(i,source) for i in source['items'].values() if not i['archived'] and i['content'].get('house_model_key')==key]
    # Deliberate commercial allowlist: no economics, deal history, client names,
    # unreviewed attachments, notes or manufacturing BOMs in the sales response.
    fields=('characteristics_md','planning_md','configurations_md','modules_md')
    return {**model_brief(db,model,definition), 'sections':{f:getattr(model,f,None) for f in fields},
            'items':items, 'connections':model_connections(db,user,key)}


@router.get('/catalog/model-connections/{key}')
def house_connections(key: str, db: Session=Depends(get_db), user=Depends(get_current_user)):
    if not user.has_access(Module.HOUSE_MODELS):
        s.fail('Нет доступа к карточкам домов',403)
    if not db.query(HouseModelCard).filter_by(key=key).first():
        s.fail('Дом не найден',404)
    return {'connections':model_connections(db,user,key)}
