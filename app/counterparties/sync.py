"""Read-only source adapter and explicit, field-level merge previews."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from uuid import UUID, uuid4, uuid5, NAMESPACE_URL
import httpx
import re
from fastapi import HTTPException
from app.core.config import settings
from app.moysklad.models import SourceLink, SourceRecord
from .models import Preview
from .schemas import Card, Save
from . import service as s

MAIN = ('name','inn','legal_type','kpp','ogrn','legal_address','actual_address','director')


def contact_key(field,value):
    value=str(value or '').strip()
    return re.sub(r'[^0-9]','',value) if field=='phone' else value.casefold()


def source_id(db, cp):
    links = db.query(SourceLink).filter(SourceLink.source_key.like('counterparty/%')).all()
    ids = {r.source_key.split('/')[-1] for r in links if (r.target_type=='counterparties' and r.target_id==cp.id) or (cp.supplier_id and r.target_type=='suppliers' and r.target_id==cp.supplier_id)}
    if len(ids)!=1:
        raise HTTPException(409,'Нет однозначной связи с МойСклад. Сначала проверьте сопоставление контрагента; по названию связь не создаётся.')
    try: return str(UUID(ids.pop()))
    except ValueError: raise HTTPException(409,'Некорректный идентификатор связи с МойСклад; требуется проверка сопоставления') from None


def fetch_live(id):
    base='https://api.moysklad.ru/api/remap/1.2/entity/counterparty/'+str(UUID(id))
    with httpx.Client(headers={'Authorization':'Bearer '+settings.moysklad_token,'Accept-Encoding':'gzip'},timeout=25,follow_redirects=False) as client:
        def read(path,params=None):
            if not settings.moysklad_token and settings.moysklad_mcp_configured:
                from app.core.mcp_remote import McpTarget, client_for, McpError
                remote=client_for(McpTarget('counterparty-requisites',settings.moysklad_mcp_url,settings.moysklad_mcp_client_id,settings.moysklad_mcp_client_secret,settings.moysklad_mcp_scope))
                try:
                    result=remote.call_tool('raw_get',{'path':'entity/counterparty/'+str(UUID(id))+path,'params':params or {}},timeout=30)
                    if not isinstance(result,dict):raise HTTPException(503,'Коннектор вернул неподдерживаемый формат данных')
                    return result
                except McpError:raise HTTPException(503,'Коннектор МойСклад недоступен; изменения не применены') from None
            try:
                response=client.get(base+path,params=params)
                if response.status_code!=200: raise HTTPException(503,'МойСклад не вернул данные. Проверьте доступ или повторите позже. Код: '+str(response.status_code))
                return response.json()
            except (httpx.HTTPError, ValueError): raise HTTPException(503,'Не удалось получить данные из МойСклад; изменения не применены') from None
        raw=read('')
        for name in ('accounts','contactpersons'):
            rows=[]
            for offset in range(0,10000,100):
                page=read('/'+name,{'limit':100,'offset':offset});batch=page.get('rows',[]);rows.extend(batch)
                if len(batch)<100:break
            else:raise HTTPException(503,'Слишком большой список реквизитов: требуется отдельная загрузка')
            raw[name]={'rows':rows}
        return raw


def source(db,cp,mode=None):
    id=source_id(db,cp)
    mode=mode or ('live' if settings.moysklad_token or settings.moysklad_mcp_configured else 'snapshot')
    if mode=='live':
        if not settings.moysklad_token and not settings.moysklad_mcp_configured:raise HTTPException(503,'Доступ к МойСклад сейчас не настроен')
        return fetch_live(id),{'kind':'moysklad_live','source_id':id,'fetched_at':datetime.now(timezone.utc).isoformat()}
    row=db.query(SourceRecord).filter_by(entity_type='counterparty',source_id=id).first()
    if not row:raise HTTPException(404,'Сохранённый снимок контрагента не найден')
    return deepcopy(row.payload),{'kind':'moysklad_snapshot','source_id':id,'fetched_at':row.imported_at.isoformat()}


def mapped(raw,current):
    data={key:raw.get(source) or '' for key,source in [('name','name'),('inn','inn'),('kpp','kpp'),('ogrn','ogrn'),('legal_address','legalAddress'),('actual_address','actualAddress')]}
    data['legal_type']={'legal':'legal_entity','entrepreneur':'sole_proprietor','individual':'individual'}.get(raw.get('companyType'),'')
    warnings=[]
    if not data['legal_type']:warnings.append('Тип контрагента не определён источником; выберите его вручную.')
    for field,source_field,fields in [('accounts','accounts',{'account_number':'accountNumber','bik':'bic','bank_name':'bankName','correspondent_account':'correspondentAccount','bank_address':'bankLocation'}),
                                     ('contacts','contactpersons',{'name':'name','phone':'phone','email':'email'})]:
        collection=raw.get(source_field,{})
        if isinstance(collection,list):rows=collection
        else:rows=collection.get('rows',[]) if isinstance(collection,dict) else []
        if not isinstance(collection,list) and (not isinstance(collection,dict) or 'rows' not in collection):warnings.append('В снимке не загружен полный список: '+('банковские счета' if field=='accounts' else 'контактные лица'))
        values=[]
        for r in rows:
            rid=str(uuid5(NAMESPACE_URL,'moysklad:'+str(raw.get('id'))+':'+field+':'+str(r.get('id'))))
            values.append({'id':rid,**{k:str(r.get(v) or '') for k,v in fields.items()},'primary':False,'archived':False})
        if field=='contacts':
            general={k:raw.get(k) or '' for k in ('phone','email')}
            for key,value in general.items():
                if value and any(contact_key(key,r.get(key))==contact_key(key,value) for r in values):general[key]=''
            if any(general.values()):
                values.append({'id':str(uuid5(NAMESPACE_URL,'moysklad:'+str(raw.get('id'))+':general')),'name':'',**general,'primary':False,'archived':False})
        for r in values:
            candidates=[old for old in current[field] if old['id']==r['id']]
            if not candidates:
                candidates=[old for old in current[field] if not old['archived'] and (
                    (field=='accounts' and r['account_number'] and old['account_number']==r['account_number'] and old['bik']==r['bik']) or
                    (field=='contacts' and any(contact_key(key,r.get(key)) and contact_key(key,r.get(key))==contact_key(key,old.get(key)) and sum(contact_key(key,v.get(key))==contact_key(key,r.get(key)) for v in values)==1 for key in ('phone','email'))))]
            if len(candidates)==1:r['id']=candidates[0]['id']
            elif len(candidates)>1:warnings.append('Несколько локальных контактов или счетов совпадают с источником: проверьте строки перед применением.')
        data[field]=values
    contacts=raw.get('contactpersons',{})
    directors=[r.get('name') for r in (contacts.get('rows',[]) if isinstance(contacts,dict) else contacts) if str(r.get('position','')).strip().lower() in ('директор','генеральный директор') and r.get('name')]
    if len(set(directors))==1:data['director']=directors[0]
    else:warnings.append('Директор не указан однозначно: нужен документ или настроенный атрибут.')
    return data,warnings


def differences(current,remote):
    result=[]
    for key in MAIN:
        value=remote.get(key)
        if value and value!=current.get(key):result.append({'key':key,'old':current.get(key,''),'value':value,'fill_empty':not current.get(key)})
    for kind in ('accounts','contacts'):
        old={r['id']:r for r in current[kind]}
        for row in remote[kind]:
            for key,value in row.items():
                if key in ('id','archived','primary') or not value:continue
                before=old.get(row['id'],{}).get(key,'')
                if value!=before:result.append({'key':kind+'.'+row['id']+'.'+key,'old':before,'value':value,'fill_empty':not before})
    return result


def proposed(current,changes,selected):
    data=deepcopy(current)
    from .schemas import Bank,Person
    for change in changes:
        if change['key'] not in selected:continue
        parts=change['key'].split('.')
        if len(parts)==1:data[parts[0]]=change['value']
        else:
            kind,id,key=parts
            row=next((r for r in data[kind] if r['id']==id),None)
            if row is None:
                row=(Bank(id=id) if kind=='accounts' else Person(id=id)).model_dump(mode='json');data[kind].append(row)
            row[key]=change['value']
    if data['legal_address']!=current['legal_address'] or data['actual_address']!=current['actual_address']:data['addresses_same']=False
    return data


def preview(db,user,id):
    cp=s.get(db,id,user,True);before=s.out(db,cp,user)
    raw,origin=source(db,cp)
    if raw.get('inn') and cp.inn and str(raw['inn']).strip()!=cp.inn:raise HTTPException(409,'ИНН в источнике отличается. Проверьте связь контрагента до обновления.')
    remote,warnings=mapped(raw,before['card']);changes=differences(before['card'],remote)
    if raw.get('archived'):warnings.append('В МойСклад карточка архивная. Локальный статус не изменяется.')
    data={'etag':before['etag'],'source':{**origin,'sha256':s.digest(raw)},'changes':changes,'warnings':warnings}
    row=Preview(id=str(uuid4()),counterparty_id=cp.id,actor_id=user.id,data={**data,'source_payload':raw});db.add(row);db.commit()
    return {'id':row.id,**data}


def apply(db,user,id,body):
    s.lock(db)
    request={'id':id,'sync':body.model_dump(mode='json')}
    repeated=s.replay(db,user,body.request_id,request)
    if repeated:return repeated
    cp=s.get(db,id,user,True)
    row=db.get(Preview,str(body.preview_id))
    if not row or row.counterparty_id!=id or row.actor_id!=user.id:raise HTTPException(404,'Сравнение не найдено')
    created=row.created_at.replace(tzinfo=timezone.utc) if row.created_at.tzinfo is None else row.created_at
    if datetime.now(timezone.utc)-created>timedelta(minutes=15):raise HTTPException(409,'Сравнение устарело. Загрузите данные ещё раз.')
    before=s.out(db,cp,user)
    if before['etag']!=row.data['etag']:raise HTTPException(409,'Карточка изменилась после сравнения')
    raw,origin=source(db,cp,'live' if row.data['source']['kind']=='moysklad_live' else 'snapshot')
    if s.digest(raw)!=row.data['source']['sha256']:raise HTTPException(409,'Источник изменился после сравнения')
    keys={c['key'] for c in row.data['changes']}
    if not set(body.fields)<=keys:raise HTTPException(422,'В сравнении нет выбранных полей')
    data=proposed(before['card'],row.data['changes'],set(body.fields))
    return s.save(db,user,Save(request_id=body.request_id,etag=before['etag'],card=Card(**data)),id,
                  provenance={**row.data['source'],'preview_id':row.id,'fields':body.fields},request=request)
