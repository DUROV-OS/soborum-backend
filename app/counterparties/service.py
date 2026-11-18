from copy import deepcopy
import hashlib
import json
from uuid import uuid5, NAMESPACE_URL
from fastapi import HTTPException
from sqlalchemy import or_, text
from app.accounting.models import Counterparty, CounterpartyKind, MoneyMovement, MoneyMovementStatus
from app.warehouse.models import Supplier
from app.common.module_access import AccessLevel, Module
from .models import Details, Account, Contact, Change
from .schemas import Card, validate


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def lock(db):
    if db.get_bind().dialect.name == 'postgresql':
        db.execute(text('select pg_advisory_xact_lock(1811202602)'))


def allowed(user, cp=None, edit=False):
    level = AccessLevel.EDIT if edit else AccessLevel.VIEW
    return user.access_level(Module.ACCOUNTING) >= level or (user.access_level(Module.WAREHOUSE) >= level and (cp is None or cp.supplier_id is not None))


def get(db, id, user, edit=False):
    query = db.query(Counterparty).filter_by(id=id)
    if edit: query = query.with_for_update()
    cp = query.populate_existing().first()
    if cp is None or not allowed(user, cp): raise HTTPException(404, 'Контрагент не найден или доступ закрыт')
    if edit and not allowed(user, cp, True): raise HTTPException(403, 'Нет прав на изменение контрагента')
    return cp


def card(db, cp):
    profile = db.get(Details, cp.id)
    data = {**Card().model_dump(mode='json'), **(profile.data if profile else {}), 'name':cp.name, 'inn':cp.inn or ''}
    for name, model in [('accounts', Account), ('contacts', Contact)]:
        data[name] = [{'id':r.id, **r.data} for r in db.query(model).filter_by(counterparty_id=cp.id).order_by(model.id)]
    if profile is None and cp.supplier_id and not data['contacts']:
        supplier = db.get(Supplier, cp.supplier_id)
        for index, contact in enumerate((supplier.contacts if supplier else None) or []):
            kind = contact.get('kind')
            if kind not in ('phone', 'email'): continue
            data['contacts'].append({'id':str(uuid5(NAMESPACE_URL,f'legacy-supplier:{cp.supplier_id}:contact:{index}')),
                'name':contact.get('person') or '', 'phone':contact.get('value','') if kind=='phone' else '',
                'email':contact.get('value','') if kind=='email' else '', 'primary':False,'archived':False})
    return data


def out(db, cp, user):
    from .verification import state
    data = card(db, cp)
    profile = db.get(Details, cp.id)
    return {'id':cp.id, 'supplier_id':cp.supplier_id, 'client_id':cp.client_id, 'kind':cp.kind.value,
            'active':cp.is_active, 'version':profile.version if profile else 0, 'card':data,
            'etag':digest({'data':data, 'active':cp.is_active, 'supplier_id':cp.supplier_id, 'client_id':cp.client_id}),
            'complete':bool(data['inn'] and data['legal_type']), 'can_edit':allowed(user, cp, True), 'bank_verification':state(db,cp,user)}


def replay(db, user, request_id, request):
    old = db.query(Change).filter_by(actor_id=user.id, request_id=str(request_id)).first()
    if old:
        if old.request_hash != digest(request): raise HTTPException(409, 'Номер запроса уже использован с другими данными')
        cp = get(db, old.counterparty_id, user, True)
        return out(db, cp, user)


def uniqueness(db, data, id=None):
    from app.accounting.service import _assert_inn_free
    _assert_inn_free(db, data['inn'], exclude_id=id)
    if data['ogrn']:
        for row in db.query(Details).filter(Details.counterparty_id != (id or 0)):
            if row.data.get('ogrn') == data['ogrn']:
                raise HTTPException(409, {'detail':'Контрагент с таким ОГРН уже существует', 'fields':{'ogrn':'ОГРН уже используется'}})


def persist(db, cp, data):
    cp.name, cp.inn = data['name'], data['inn'] or None
    profile = db.get(Details, cp.id)
    if profile is None:
        profile = Details(counterparty_id=cp.id, version=0, data={}); db.add(profile)
    profile.data = {k:v for k,v in data.items() if k not in ('name','inn','accounts','contacts')}
    profile.version += 1
    for field, model in [('accounts',Account),('contacts',Contact)]:
        existing = {r.id:r for r in db.query(model).filter_by(counterparty_id=cp.id)}
        submitted = {r['id']:r for r in data[field]}
        # Omitted rows are archived, not physically deleted: historical references survive.
        for id, row in existing.items():
            if id not in submitted: row.data = {**row.data, 'archived':True, 'primary':False}
        for id, value in submitted.items():
            row = existing.get(id)
            if row is None:
                if db.get(model,id): raise HTTPException(409,'Идентификатор строки принадлежит другой карточке')
                row = model(id=id,counterparty_id=cp.id,data={});db.add(row)
            row.data = {k:v for k,v in value.items() if k!='id'}
    db.flush()


def save(db, user, body, id=None, provenance=None, request=None):
    lock(db)
    request = request or {'id':id, **body.model_dump(mode='json')}
    old = replay(db,user,body.request_id,request)
    if old: return old
    if not allowed(user,edit=True): raise HTTPException(403,'Нет прав на изменение справочника')
    cp = get(db,id,user,True) if id else None
    before = out(db,cp,user) if cp else None
    if cp and body.etag != before['etag']: raise HTTPException(409,'Карточку изменили. Обновите данные перед сохранением.')
    data = body.card.model_dump(mode='json')
    validate(data, before['card'] if before else None)
    uniqueness(db,data,id)
    if cp and data['inn'] != (cp.inn or '') and db.query(MoneyMovement.id).filter_by(counterparty_id=cp.id,status=MoneyMovementStatus.POSTED).first():
        raise HTTPException(409,'У контрагента есть проведённые платежи. Изменение ИНН требует отдельной проверки бухгалтером.')
    if cp is None:
        if not body.supplier and user.access_level(Module.ACCOUNTING)<AccessLevel.EDIT: raise HTTPException(403,'Для других контрагентов нужен доступ к бухгалтерии')
        supplier = Supplier(name=data['name'], categories=[], contacts=[]) if body.supplier else None
        if supplier: db.add(supplier);db.flush()
        cp=Counterparty(name=data['name'],inn=data['inn'] or None,kind=CounterpartyKind.SUPPLIER if supplier else CounterpartyKind.OTHER,supplier_id=supplier.id if supplier else None)
        db.add(cp);db.flush()
    persist(db,cp,data)
    # Keep the legacy supplier title in sync without touching prices, MAX or contacts.
    if cp.supplier_id:
        supplier=db.get(Supplier,cp.supplier_id)
        supplier.name=data['name']
    after=out(db,cp,user)
    db.add(Change(counterparty_id=cp.id,actor_id=user.id,request_id=str(body.request_id),request_hash=digest(request),
                  data={'before':before,'after':after,'source':provenance or {'kind':'manual'}}))
    db.commit()
    return after


def registry(db,user,q='',offset=0,limit=50):
    if not allowed(user): raise HTTPException(403,'Нет доступа к справочнику контрагентов')
    query=db.query(Counterparty)
    if user.access_level(Module.ACCOUNTING)<AccessLevel.VIEW: query=query.filter(Counterparty.supplier_id.is_not(None))
    if q.strip():
        term='%'+q.strip().replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
        query=query.filter(or_(Counterparty.name.ilike(term,escape='\\'),Counterparty.inn.ilike(term,escape='\\')))
    return {'items':[out(db,r,user) for r in query.order_by(Counterparty.name,Counterparty.id).offset(offset).limit(limit)],
            'total':query.count(),'can_create':allowed(user,edit=True)}
