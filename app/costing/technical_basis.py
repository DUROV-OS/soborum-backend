"""Construct quantities and source references without calculating prices."""
from copy import deepcopy
from decimal import Decimal as D
import hashlib, json
from app.costing import service as s


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), default=str).encode()).hexdigest()


def number(value):
    return format(D(str(value)).normalize(), 'f')


def item_basis(item, source):
    data=item['content']
    keys=('source_recipe_id','source_recipe_version','warehouse_product_id','house_model_key','unit','mode','area','length','width','modules','note')
    result={k:data.get(k) for k in keys}
    result['service_only']=data.get('service_only',False)
    result.update(id=item['id'],title=item['title'],kind=item['kind'],archived=item['archived'])
    for key in ('lines','replacements'):
        result[key]=[]
        for line in data.get(key,[]):
            mat=source['materials'].get(str(line['material_id']))
            if not mat: s.fail('Материал расчёта больше не существует')
            result[key].append({'material_id':mat['id'],'warehouse_material_id':mat.get('warehouse_material_id'),
                'name':mat['name'],'unit':mat['unit'],'archived':mat['archived'],
                'quantity':number(D(line['quantity'])*line.get('pieces',1))})
        result[key].sort(key=lambda row:row['material_id'])
    result['addons']=[{'addon_id':r['addon_id'],'quantity':number(r['quantity'])} for r in data.get('addons',[])]
    return result


def prepare(payload, source):
    if sum((bool(payload.discount),payload.sale_price is not None,payload.discount_amount is not None))>1:
        s.fail('Выберите один способ: скидка в процентах, скидка в рублях или договорная цена')
    source=deepcopy(source)
    base=deepcopy(source['items'].get(str(payload.template_id)))
    if not base or base['kind']!='template' or base['archived']: s.fail('Выберите действующую типовую смету')
    service_only=base['content'].get('service_only',False)
    if service_only and (payload.lines or payload.selections): s.fail('Услуга без складского учёта не может содержать материалы или опции')
    if payload.lines is not None: base['content']['lines']=[x.model_dump(mode='json') for x in payload.lines]
    parts=[(base,D(1),'base','')]
    selected=[]
    for selection in payload.selections:
        chosen=source['items'].get(str(selection.item_id))
        if not chosen or chosen['archived'] or chosen['kind'] not in ('addon','package'): s.fail('Выберите действующий доп или пакет')
        selected.append(item_basis(chosen,source))
        rows=chosen['content']['addons'] if chosen['kind']=='package' else [{'addon_id':chosen['id'],'quantity':'1'}]
        if not rows: s.fail('Пакет не содержит опций')
        for row in rows:
            addon=source['items'].get(str(row['addon_id']))
            if not addon or addon['archived'] or addon['kind']!='addon': s.fail('Пакет содержит недействующую опцию')
            parts.append((addon,selection.quantity*D(row['quantity']),'addon',chosen['title'] if chosen['kind']=='package' else ''))
    available, consumed, bom, commercial={}, {}, {}, []
    for item,count,kind,package in parts:
        basis=item_basis(item,source)
        if not basis['lines'] and not service_only: s.fail('Не заполнен состав: '+item['title'])
        if any(row['archived'] for row in basis['lines']+basis['replacements']): s.fail('В составе есть архивный материал')
        commercial.append({'item_id':item['id'],'kind':kind,'title':item['title'],'package':package,
                           'quantity':number(count*payload.quantity),'unit_price':None,'amount':None})
        for sign,key in ((1,'lines'),(-1,'replacements')):
            for line in basis[key]:
                mid=line['material_id']; quantity=D(line['quantity'])*count
                if kind=='base' and sign==1: available[mid]=available.get(mid,D(0))+quantity
                if sign==-1:
                    consumed[mid]=consumed.get(mid,D(0))+quantity
                    if consumed[mid]>available.get(mid,D(0)): s.fail('Допы заменяют больше базового материала, чем есть в смете')
                if mid not in bom: bom[mid]={**line,'quantity':D(0),'price':None,'amount':None,'category':''}
                bom[mid]['quantity']+=sign*quantity*payload.quantity
    lines=[{**line,'quantity':number(line['quantity'])} for _,line in sorted(bom.items()) if line['quantity']>0]
    if not lines and not service_only: s.fail('После замен состав изделия пуст')
    basis={'quantity':number(payload.quantity),'template':item_basis(base,source),
           'parts':[{'item':item_basis(item,source),'quantity':number(count)} for item,count,_,_ in parts],
           'selections':selected,'client_id':payload.client_id,'crm_deal_id':payload.crm_deal_id,'note':payload.note,'bom':lines}
    item_ids={str(item['id']) for item,_,_,_ in parts}|{str(x.item_id) for x in payload.selections}
    source['items']={key:value for key,value in source['items'].items() if key in item_ids}
    material_ids={str(row['material_id']) for item,_,_,_ in parts for key in ('lines','replacements') for row in item['content'].get(key,[])}
    source['materials']={key:value for key,value in source['materials'].items() if key in material_ids}
    snapshot={'input':payload.model_dump(mode='json'),'sources':source,'technical_basis':basis,'technical_fingerprint':fingerprint(basis),
        'result':{'status':'awaiting_production','missing':['Состав и работы должен согласовать начальник производства'],
            **{key:None for key in ('price','list_price','cost','profit','margin_percent','minimum_price','discount_amount','base_cost','rate_cost')},
            'target_margin_percent':'35','revenue_expenses':[]},'commercial':commercial,'bom':lines,
        'profile':payload.profile.model_dump(mode='json') if payload.profile else s.profile_for(base,source['settings']),
        'expenses':payload.expenses.model_dump(mode='json') if payload.expenses else base['content']['expenses'],
        'image':payload.image if payload.image is not None else base['content'].get('image',''),
        'company':source['settings']['company'],'logo':source['settings']['logo']}
    return snapshot
