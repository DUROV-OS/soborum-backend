"""Native links: use existing catalog/recipes/orders; freeze norms only for an accepted order."""
from copy import deepcopy
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
import hashlib,json
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from pydantic import Field
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.common.module_access import AccessLevel, Module
from app.core.deps import get_current_user
from app.costing import service as s
from app.costing.models import CostingEstimate, CostingItem, CostingMaterial, CostingOrderLink, CostingRevision
from app.costing.schemas import Strict, VersionAction, ItemData
from app.db.session import get_db
from app.moysklad.recipes import Recipe, resolve_lines
from app.sales.models import SalesOrder
from app.users.models import User, UserRole
from app.warehouse.models import Warehouse, WarehouseMaterial

router=APIRouter()


@router.get('/integration/catalog')
def integration_catalog(db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    s.require_cost(user)
    from app.house_models.models import HouseModelCard
    return {'models':[{'key':m.key,'title':m.title} for m in db.query(HouseModelCard).order_by(HouseModelCard.title)] if user.has_access(Module.HOUSE_MODELS) else []}


def module(user, name, level=AccessLevel.VIEW):
    if user.access_level(name)<level:
        s.fail("Нет доступа к связанному разделу",403)


def read(user:User=Depends(get_current_user)):
    s.can_estimate(user);return user


def engineering(user:User=Depends(get_current_user)):
    s.require_cost(user,AccessLevel.EDIT)
    if not (user.has_access(Module.PRODUCTION) or user.has_access(Module.WAREHOUSE)):
        s.fail("Нужен доступ к техкартам",403)
    return user


def commercial_engineering(user:User=Depends(get_current_user)):
    s.require_cost(user,AccessLevel.EDIT);module(user,Module.CLIENTS,AccessLevel.EDIT);return user


def context_allowed(user,kind):
    if kind in ('client','deal','order'):module(user,Module.CLIENTS)
    elif kind=='recipe':
        if user.role!=UserRole.ADMIN and user.job_role=='manager':s.fail("Нет доступа к техкартам",403)
        if not (user.has_access(Module.PRODUCTION) or user.has_access(Module.WAREHOUSE)):s.fail("Нет доступа к техкартам",403)
    elif kind=='model':module(user,Module.HOUSE_MODELS)
    elif kind=='product':module(user,Module.WAREHOUSE)
    else:s.fail("Неизвестный контекст")


def validate_context(db,body,user):
    if body.crm_deal_id:
        module(user,Module.CLIENTS)
        from app.crm.models import CRMEntity
        deal=db.get(CRMEntity,body.crm_deal_id)
        if not deal or deal.kind!='deal' or deal.archived:s.fail("Выберите действующую сделку")
    if body.sales_order_id is not None:
        module(user,Module.CLIENTS)
        order=db.get(SalesOrder,body.sales_order_id)
        if not order or order.archived or order.status=='cancelled':s.fail("Заказ недоступен")
        if body.sales_order_line is None or body.sales_order_line>=len(order.lines):s.fail("Выберите строку заказа")
    elif body.sales_order_line is not None:s.fail("Не указан заказ для строки")


def validate_item_sources(db,data):
    if data.source_recipe_id:
        recipe=db.get(Recipe,data.source_recipe_id)
        if not recipe or recipe.archived:s.fail("Техкарта недоступна")
    if data.warehouse_product_id:
        product=db.get(WarehouseMaterial,data.warehouse_product_id)
        if not product or product.is_archived:s.fail("Выберите действующее изделие общей номенклатуры")
    if data.house_model_key:
        from app.house_models.models import HouseModelCard
        if not db.query(HouseModelCard).filter_by(key=data.house_model_key).first():s.fail("Модель дома не найдена")


class RecipeImport(Strict):
    warehouse:Warehouse
    item_version:int|None=Field(None,ge=1)


@router.post('/integration/recipes/{id}/import')
def import_recipe(id:int,body:RecipeImport,db:Session=Depends(get_db),user:User=Depends(engineering)):
    s.lock(db)
    recipe=db.query(Recipe).filter_by(id=id).with_for_update().populate_existing().first()
    if not recipe:s.fail("Техкарта не найдена",404)
    inputs,outputs=resolve_lines(db,recipe,body.warehouse,Decimal(1))
    if len(outputs)!=1:s.fail("Для совместного выпуска нескольких изделий сначала подтвердите распределение затрат")
    product,output_quantity=outputs[0]
    old=db.query(CostingItem).filter(CostingItem.kind=='template',CostingItem.content['source_recipe_id'].as_integer()==id,
        CostingItem.content['warehouse_product_id'].as_integer()==product.id).first()
    if old and body.item_version is None:return {'item_id':old.id,'created':False,'updated':False}
    if old and old.version!=body.item_version:s.fail("Смета изменилась; обновите её перед переносом норматива",409)
    if old and old.archived:s.fail("Смета в архиве")
    grouped={}
    today=datetime.now(ZoneInfo('Europe/Moscow')).date()
    for material,quantity in inputs:
        quantity=quantity/output_quantity
        if quantity!=quantity.quantize(Decimal('.000001')):s.fail("Норма на одно изделие требует более шести знаков: "+material.title)
        row=db.query(CostingMaterial).filter_by(warehouse_material_id=material.id).first()
        if row and row.archived:s.fail("Восстановите материал сметы из архива: "+material.title)
        if row is None:
            row=CostingMaterial(warehouse_material_id=material.id,name=material.title,category=material.category.value,unit=material.unit,
                price=None,price_source='Общая складская закупочная цена',effective_on=today,archived=False,version=1)
            db.add(row);db.flush();s.audit(db,user,'material',row.id,'link_warehouse',None,s.material_out(db,row))
        grouped[row.id]=grouped.get(row.id,Decimal(0))+quantity
    content=ItemData.model_validate(old.content) if old else ItemData(profile=s.settings_out(db)['content']['profiles'][0]['name'])
    raw={**content.model_dump(mode='json'),'source_recipe_id':recipe.id,'source_recipe_version':recipe.version,'warehouse_product_id':product.id,
        'unit':product.unit,'lines':[{'material_id':id,'quantity':str(q),'pieces':1,'price':None,'price_reason':''} for id,q in grouped.items()]}
    try:content=ItemData.model_validate(raw)
    except ValueError:s.fail("Проверьте единицы и допустимые нормы техкарты перед переносом")
    before=s.item_out(old) if old else None
    row=old or CostingItem(kind='template',title=recipe.title,archived=False,version=0)
    row.version+=1;row.content=content.model_dump(mode='json')
    db.add(row);db.flush();s.audit(db,user,'template',row.id,'import_recipe',before,s.item_out(row));db.commit()
    return {'item_id':row.id,'created':old is None,'updated':old is not None}


def fingerprint(order,index):
    line=order.lines[index]
    data={'currency':order.currency,'material_id':line.get('material_id'),'stock_tracked':line.get('stock_tracked',True),
        'quantity':str(s.decimal(line['quantity']).normalize()),'unit_price':str(s.decimal(line['unit_price']).normalize()),
        'line_total':str(s.decimal(line['line_total']).normalize()),'unit':line['unit']}
    return hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest()


def link_out(db,link,order=None):
    order=order or db.get(SalesOrder,link.order_id)
    revision=db.get(CostingRevision,link.revision_id)
    estimate=db.get(CostingEstimate,link.estimate_id)
    fresh=link.line_index<len(order.lines) and fingerprint(order,link.line_index)==link.fingerprint
    if order.status=='draft':fresh=fresh and estimate.version==revision.number and estimate.status=='approved' and not estimate.archived
    if order.status=='draft' and fresh:
        from app.costing.governance import review_state
        fresh=review_state(db,estimate)['director_approved']
    return {'order_id':order.id,'order_line':link.line_index,'estimate_id':estimate.id,'revision':revision.number,
        'fresh':bool(fresh),'production_recipe_id':link.recipe_id}


def guard_order(db,order):
    links=db.query(CostingOrderLink).filter_by(order_id=order.id).all()
    if not links:s.fail('Для подтверждения продажи нужна смета с согласованиями производства, бухгалтера и руководителя для каждой строки',409)
    if {l.line_index for l in links}!=set(range(len(order.lines))) or any(not link_out(db,l,order)['fresh'] for l in links):
        s.fail("Сверьте сметы всех строк заказа: цена, количество или утверждённая версия изменились",409)
    from app.costing.governance import assert_approved
    for link in links: assert_approved(db,db.get(CostingEstimate,link.estimate_id))


class OrderBinding(VersionAction):
    order_id:int=Field(gt=0)
    order_version:int=Field(ge=1)
    line_index:int=Field(ge=0,le=499)


def bind(db,user,estimate,revision,order,index):
    from app.costing.governance import assert_approved
    assert_approved(db,estimate)
    other=db.query(CostingOrderLink).filter(CostingOrderLink.estimate_id==estimate.id,
        or_(CostingOrderLink.order_id!=order.id,CostingOrderLink.line_index!=index)).first()
    if other:s.fail("Смета уже закреплена за другой строкой заказа; для отдельного заказа создайте копию сметы",409)
    current=db.query(CostingOrderLink).filter_by(order_id=order.id,line_index=index).first()
    if current and current.estimate_id==estimate.id and current.revision_id==revision.id and link_out(db,current,order)['fresh']:
        return current
    if order.status!='draft' or order.archived:s.fail("Связать смету можно с действующим черновиком заказа")
    if estimate.status!='approved' or estimate.archived or revision.snapshot['result']['status']!='meets_target':s.fail("Сначала утвердите полный расчёт сметы")
    if index>=len(order.lines):s.fail("Строка заказа не найдена")
    snap=revision.snapshot
    product=snap['sources']['items'][str(snap['input']['template_id'])]['content'].get('warehouse_product_id')
    line=order.lines[index]
    if not product or line.get('material_id')!=product:s.fail("Изделие сметы должно совпадать с общей номенклатурой строки заказа")
    if s.decimal(line['quantity'])!=s.decimal(snap['input']['quantity']) or s.decimal(line['line_total'])!=s.decimal(snap['result']['price']):
        s.fail("Количество и итоговая цена сметы должны совпадать со строкой заказа")
    old=db.query(CostingOrderLink).filter_by(order_id=order.id,line_index=index).first()
    if old and old.recipe_id:s.fail("По этой строке уже выдан производственный норматив; изменение требует отдельной версии заказа")
    link=old or CostingOrderLink(order_id=order.id,line_index=index)
    link.estimate_id=estimate.id;link.revision_id=revision.id;link.fingerprint=fingerprint(order,index);link.actor_id=user.id
    db.add(link);db.flush()
    estimate.sales_order_id=order.id;estimate.sales_order_line=index
    from app.sales.service import event
    event(db,order,user,'costing_linked',{'estimate_id':estimate.id,'revision':revision.number,'line_index':index})
    s.audit(db,user,'estimate',estimate.id,'bind_order',None,{'order_id':order.id,'line_index':index,'revision':revision.number})
    return link


@router.post('/integration/estimates/{id}/bind-order')
def bind_order(id:int,body:OrderBinding,db:Session=Depends(get_db),user:User=Depends(commercial_engineering)):
    s.lock(db)
    from app.sales.service import get_order
    estimate=s.get_estimate(db,id,user)
    if estimate.version!=body.version:s.fail("Версия сметы изменилась",409)
    from app.costing.governance import assert_approved
    assert_approved(db,estimate)
    order=get_order(db,body.order_id,True)
    if order.version!=body.order_version:s.fail("Заказ изменился",409)
    link=bind(db,user,estimate,s.get_revision(db,estimate),order,body.line_index)
    db.commit();return link_out(db,link,order)


class NewOrder(VersionAction):
    counterparty_id:int=Field(gt=0)
    organization_id:int=Field(gt=0)


@router.post('/integration/estimates/{id}/create-order',status_code=201)
def create_order(id:int,body:NewOrder,db:Session=Depends(get_db),user:User=Depends(commercial_engineering)):
    s.lock(db)
    from app.sales import service as sales
    from app.sales.schemas import CreateOrder,Line
    estimate=s.get_estimate(db,id,user)
    if estimate.version!=body.version:s.fail("Версия сметы изменилась",409)
    from app.costing.governance import assert_approved
    assert_approved(db,estimate)
    revision=s.get_revision(db,estimate);snap=revision.snapshot
    if estimate.status!='approved' or snap['result']['status']!='meets_target':s.fail("Сначала утвердите расчёт")
    existing=db.query(CostingOrderLink).filter_by(estimate_id=id).first()
    if existing:
        order=db.get(SalesOrder,existing.order_id)
        if existing.revision_id!=revision.id or order.counterparty_id!=body.counterparty_id or order.organization_id!=body.organization_id:
            s.fail("Смета уже связана с заказом. Сверьте его строку; для другого заказа создайте копию сметы",409)
        return link_out(db,existing,order)
    product=snap['sources']['items'][str(snap['input']['template_id'])]['content'].get('warehouse_product_id')
    if not product:s.fail("Свяжите типовое изделие с общей номенклатурой и актуализируйте смету")
    quantity=s.decimal(snap['input']['quantity']);price=s.decimal(snap['result']['price'])
    unit_price=(price/quantity).quantize(Decimal('.00000001'),rounding=ROUND_HALF_UP)
    if (quantity*unit_price).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)!=price:s.fail("Цена не делится на количество с допустимой точностью")
    order_body=CreateOrder(request_id=body.request_id,counterparty_id=body.counterparty_id,organization_id=body.organization_id,
        lines=[Line(material_id=product,quantity=quantity,unit_price=unit_price)],note=f'Смета №{id}, версия {revision.number}')
    result=sales.create_order(db,order_body,user,commit=False)
    order=db.get(SalesOrder,result['id'])
    link=bind(db,user,estimate,revision,order,0);db.commit();return link_out(db,link,order)


class ProductionHandoff(VersionAction):
    order_id:int=Field(gt=0)
    line_index:int=Field(ge=0,le=499)


@router.post('/integration/estimates/{id}/production-recipe')
def production_recipe(id:int,body:ProductionHandoff,db:Session=Depends(get_db),user:User=Depends(engineering)):
    module(user,Module.PRODUCTION,AccessLevel.EDIT)
    s.lock(db)
    from app.sales.service import get_order
    order=get_order(db,body.order_id,True)
    if order.archived or order.status=='cancelled':s.fail("Заказ не действует")
    link=db.query(CostingOrderLink).filter_by(order_id=order.id,line_index=body.line_index,estimate_id=id).first()
    if not link or not link_out(db,link,order)['fresh']:s.fail("Сначала свяжите актуальную утверждённую смету со строкой заказа")
    from app.costing.governance import assert_approved
    assert_approved(db,db.get(CostingEstimate,id))
    if link.recipe_id:return {'recipe_id':link.recipe_id,'created':False}
    revision=db.get(CostingRevision,link.revision_id)
    if revision.number!=body.version:s.fail("Проверьте версию производственной сметы",409)
    snap=revision.snapshot;product_id=order.lines[link.line_index]['material_id'];product=db.get(WarehouseMaterial,product_id)
    if not product or product.is_archived:s.fail("Изделие склада недоступно")
    grouped={}
    for line in snap['bom']:
        mat=snap['sources']['materials'][str(line['material_id'])]
        warehouse_id=mat.get('warehouse_material_id');stock=db.get(WarehouseMaterial,warehouse_id) if warehouse_id else None
        if not stock or stock.is_archived or stock.unit!=line['unit'] or stock.warehouse!=product.warehouse:
            s.fail("Сопоставьте с тем же складом материал производственной сметы: "+line['name'])
        quantity=s.decimal(line['quantity'])
        if quantity!=quantity.quantize(Decimal('.000001')) or (not stock.is_fractional and quantity%1):s.fail("Недопустимая норма материала: "+stock.title)
        if stock.id not in grouped:grouped[stock.id]={'material_id':stock.id,'title':stock.title,'unit':stock.unit,'quantity':Decimal(0),'source_id':None}
        grouped[stock.id]['quantity']+=quantity
    inputs=[{**line,'quantity':str(line['quantity'])} for line in grouped.values()]
    if not inputs:s.fail("Пустой состав нельзя передать в производство")
    source_key=f'costing-order:{order.id}:{link.line_index}:{revision.id}'
    recipe=db.query(Recipe).filter_by(source_id=source_key).first()
    if not recipe:
        recipe=Recipe(source_id=source_key,title=f'{order.number} · смета №{id} · версия {revision.number}',archived=False,version=1,
            content={'inputs':inputs,'outputs':[{'material_id':product.id,'source_id':None,'title':product.title,'unit':product.unit,'quantity':snap['input']['quantity']}],
                'warnings':[],'stages':[{'title':'Норматив заказа из утверждённой сметы','costing_estimate_id':id,'costing_revision':revision.number,'sales_order_id':order.id,'sales_order_line':link.line_index}]})
        db.add(recipe);db.flush()
    link.recipe_id=recipe.id
    s.audit(db,user,'estimate',id,'production_norm',None,{'order_id':order.id,'recipe_id':recipe.id,'revision':revision.number})
    db.commit();return {'recipe_id':recipe.id,'created':True}


@router.get('/context')
def context(kind:str,id:str,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    context_allowed(user,kind)
    from app.clients.models import Client
    from app.crm.models import CRMEntity
    from app.house_models.models import HouseModelCard
    model={'client':Client,'deal':CRMEntity,'order':SalesOrder,'recipe':Recipe,'product':WarehouseMaterial}.get(kind)
    obj=db.query(HouseModelCard).filter_by(key=id).first() if kind=='model' else db.get(model,int(id)) if id.isdigit() else None
    if not obj or (kind=='deal' and obj.kind!='deal'):s.fail("Связанный объект не найден",404)
    can_read=s.cost_access(user) or user.has_access(Module.CLIENTS)
    q=s.estimate_query(db,user) if can_read else db.query(CostingEstimate).filter(False)
    if kind=='client':q=q.filter(CostingEstimate.client_id==obj.id)
    elif kind=='deal':q=q.filter(CostingEstimate.crm_deal_id==obj.id)
    elif kind=='order':q=q.filter(or_(CostingEstimate.sales_order_id==obj.id,CostingEstimate.id.in_(db.query(CostingOrderLink.estimate_id).filter_by(order_id=obj.id))))
    else:
        field={'recipe':'source_recipe_id','product':'warehouse_product_id','model':'house_model_key'}[kind]
        item_ids=[r.id for r in db.query(CostingItem) if r.content.get(field)==(id if kind=='model' else obj.id)]
        q=q.join(CostingRevision,(CostingRevision.estimate_id==CostingEstimate.id)&(CostingRevision.number==CostingEstimate.version)).filter(CostingRevision.snapshot['input']['template_id'].as_integer().in_(item_ids))
    estimates=[s.estimate_out(db,row,user) for row in q.order_by(CostingEstimate.id.desc()).limit(100)]
    templates=[]
    for item in db.query(CostingItem).filter_by(kind='template',archived=False):
        data=item.content
        matches=(kind=='recipe' and data.get('source_recipe_id')==obj.id) or (kind=='product' and data.get('warehouse_product_id')==obj.id) or (kind=='model' and data.get('house_model_key')==id)
        if matches:templates.append({'id':item.id,'title':item.title,'version':item.version,'source_version':data.get('source_recipe_version')})
    links=db.query(CostingOrderLink)
    links=links.filter_by(order_id=obj.id) if kind=='order' else links.filter_by(recipe_id=obj.id) if kind=='recipe' else links.filter(False)
    link_data=[link_out(db,l) for l in links]
    if not can_read:
        estimates=[]
    order_lines=None
    if kind=='order':order_lines=[{'index':i,'title':l['title'],'quantity':l['quantity'],'price':l['line_total'],'material_id':l.get('material_id')} for i,l in enumerate(obj.lines)]
    return {'kind':kind,'id':id,'title':getattr(obj,'full_name',getattr(obj,'name',getattr(obj,'title',getattr(obj,'number','')))),
        'estimates':estimates,'templates':templates,'links':link_data,'order_version':obj.version if kind=='order' else None,'order_lines':order_lines,
        'can_import_recipe':kind=='recipe' and s.cost_access(user,AccessLevel.EDIT)}
