"""Versioned documentary classification, independent of cash posting and cost recognition."""
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from fastapi import HTTPException
from sqlalchemy.orm import selectinload
from app.accounting.models import MoneyMovement, MoneyDirection, MoneyMovementStatus, Organization, BankAccount
from app.accounting.evidence_models import PaymentEvidenceDocument as Document, PaymentEvidenceVersion as Version, PaymentEvidenceCheck as Check
from app.accounting.evidence_schemas import CATEGORIES
from app.common.files import FileAsset, FilePurpose, file_integrity
from app.costing.governance import responsible
from app.costing.models import CostingEstimate
from app.users.models import User


def fail(message, code=422):
    raise HTTPException(code, detail=message)


def canonical(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()


def decimal(value):
    return Decimal(str(value)).quantize(Decimal('.01'))


def source(row):
    # Posting a real bank movement is not documentary approval. Posting timestamps/status
    # may advance without changing the payment being checked; cancellation invalidates it.
    keys=('amount','tax','currency','direction','subkind','assessment','affects_profit','account_id',
          'counterparty_id','source_kind','client_id','employee_id','supply_id','purchase_return_id',
          'external_number','doc_date','payment_purpose')
    result={'id':row.id}
    for key in keys:
        value=getattr(row,key)
        if key in ('amount','tax'): value=str(decimal(value))
        elif hasattr(value,'value'): value=value.value
        elif isinstance(value,datetime):
            value=value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
            value=value.isoformat()
        result[key]=value
    return result


def movement(db, id, lock=False):
    query=db.query(MoneyMovement).filter_by(id=id)
    if lock: query=query.with_for_update(of=MoneyMovement).populate_existing()
    row=query.first()
    if not row: fail('Платёж не найден',404)
    if row.direction != MoneyDirection.EXPENSE: fail('Проверка предназначена для исходящих платежей')
    return row


def documents(db,row):
    scoped=db.query(FileAsset).join(Document,Document.file_id==FileAsset.id).filter(Document.movement_id==row.id).all()
    return {f.id:f for f in [*row.documents,*scoped] if f.purpose==FilePurpose.MONEY_MOVEMENT_DOCUMENT}


def checked_file(asset, cache=None):
    if cache is None: return file_integrity(asset)
    key=(asset.id,asset.sha256,asset.size_bytes)
    if key not in cache: cache[key]=file_integrity(asset)
    return cache[key]


def proofs(db,row,body):
    available=documents(db,row)
    result={}
    for citation in [body.payment_evidence,*[a.evidence for a in body.allocations]]:
        asset=available.get(citation.file_id)
        if not asset: fail('Документ должен принадлежать этому платежу')
        if checked_file(asset)!='matches': fail('Документ отсутствует, изменён или не имеет исходной контрольной суммы')
        result[str(asset.id)]={'sha256':asset.sha256,'size_bytes':asset.size_bytes,'filename':asset.filename}
    return result


def latest(db,id):
    return db.query(Version).filter_by(movement_id=id).order_by(Version.version.desc()).first()


def state(db,row,version=None,check=None,cache=None,known=False,available=None,person=None):
    if row.status==MoneyMovementStatus.CANCELLED:
        return {'state':'cancelled','issues':['Платёж отменён'],'version':version.version if version else 0}
    if version is None and not known: version=latest(db,row.id)
    if available is None: available=documents(db,row)
    if not version:
        has_docs=bool(available)
        return {'state':'unclassified' if has_docs else 'no_document','issues':['Укажите статьи, проект или месяц и документальные основания'] if has_docs else ['Загрузите документ или скан'],'version':0}
    issues=[]
    if version.source_hash!=canonical(source(row)): issues.append('Реквизиты или сумма платежа изменились после разнесения')
    for file_id,proof in version.content['proofs'].items():
        asset=available.get(int(file_id))
        if not asset or asset.sha256!=proof['sha256'] or asset.size_bytes!=proof['size_bytes'] or checked_file(asset,cache)!='matches':
            issues.append('Не подтверждена целостность документа: '+proof['filename'])
    if issues: return {'state':'stale','issues':issues,'version':version.version}
    if check is None and not known: check=db.query(Check).filter_by(version_id=version.id).first()
    if not known: person=responsible(db,'accounting')
    if check and (check.content.get('version_hash')!=canonical(version.content) or check.content.get('source_hash')!=version.source_hash):
        return {'state':'stale','issues':['Согласование не соответствует версии разнесения'],'version':version.version}
    if check and person and check.actor_id==person.id:
        return {'state':'verified','issues':[],'version':version.version,'checked_by':person.full_name,'checked_at':check.created_at}
    return {'state':'pending','issues':['Ожидает личной проверки бухгалтера'],'version':version.version}


def save_version(db,row,body,user):
    if row.status==MoneyMovementStatus.CANCELLED: fail('Отменённый платёж нельзя согласовать')
    data=body.model_dump(mode='json')
    old=db.query(Version).filter_by(request_id=str(body.request_id)).first()
    if old:
        if old.movement_id!=row.id or old.actor_id!=user.id or old.content['request']!=data:
            fail('Повторный запрос отличается от сохранённого',409)
        if old.source_hash!=canonical(source(row)): fail('Платёж изменился; создайте новую версию',409)
        return old
    current=latest(db,row.id)
    if body.expected_version!=(current.version if current else 0): fail('Разнесение изменилось; обновите карточку',409)
    if body.source_hash!=canonical(source(row)): fail('Платёж изменился; обновите карточку',409)
    if sum((a.amount for a in body.allocations),Decimal(0))!=decimal(row.amount):
        fail('Сумма распределения должна точно совпадать с суммой платежа')
    for allocation in body.allocations:
        if allocation.project_id and not db.get(CostingEstimate,allocation.project_id): fail('Смета проекта не найдена')
        if body.nature in ('refund','transfer') and allocation.category!='not_cost':
            fail('Возврат и перевод не относятся к затратам')
        if body.nature=='expense' and allocation.category=='not_cost': fail('Для оплаты расхода выберите статью затрат')
    proof=proofs(db,row,body)
    result=Version(movement_id=row.id,version=body.expected_version+1,actor_id=user.id,request_id=str(body.request_id),
                   source_hash=body.source_hash,content={'request':data,'source':source(row),'proofs':proof})
    db.add(result);db.flush();return result


def sign(db,row,body,user):
    person=responsible(db,'accounting')
    if not person or user.id!=person.id: fail('Проверку лично выполняет назначенный бухгалтер',403)
    version=latest(db,row.id)
    if not version or version.version!=body.version: fail('Версия разнесения изменилась; обновите карточку',409)
    current=state(db,row,version)
    if current['state'] not in ('pending','verified'): fail('Нельзя подтвердить: '+'; '.join(current['issues']),409)
    data=body.model_dump(mode='json')
    existing=db.query(Check).filter_by(request_id=str(body.request_id)).first()
    if existing:
        if existing.version_id!=version.id or existing.actor_id!=user.id or existing.content['request']!=data:
            fail('Повторный запрос отличается от сохранённого',409)
        return existing
    if db.query(Check).filter_by(version_id=version.id).first(): fail('Эта версия уже проверена',409)
    check=Check(version_id=version.id,actor_id=user.id,request_id=str(body.request_id),
                content={'request':data,'version_hash':canonical(version.content),'source_hash':version.source_hash})
    db.add(check);db.flush();return check


def detail(db,row,user,can_edit):
    versions=db.query(Version).filter_by(movement_id=row.id).order_by(Version.version.desc()).all()
    checks={c.version_id:c for c in db.query(Check).filter(Check.version_id.in_([v.id for v in versions])).all()} if versions else {}
    person=responsible(db,'accounting')
    users={u.id:u.full_name for u in db.query(User).filter(User.id.in_({v.actor_id for v in versions}|{c.actor_id for c in checks.values()})).all()}
    return {'movement':source(row),'status':row.status.value,'source_hash':canonical(source(row)),
            'counterparty':row.counterparty.name if row.counterparty else None,
            'account':row.account.name if row.account else None,
            'review':state(db,row,versions[0] if versions else None),
            'responsible':{'id':person.id,'name':person.full_name} if person else None,
            'can_edit':can_edit,'can_check':can_edit and person is not None and person.id==user.id,
            'documents':[{'id':f.id,'filename':f.filename,'sha256':f.sha256,'size_bytes':f.size_bytes,
                          'integrity':checked_file(f),'created_at':f.created_at} for f in documents(db,row).values()],
            'versions':[{'version':v.version,'created_at':v.created_at,'actor':users.get(v.actor_id),'data':v.content['request'],
                         'proofs':v.content['proofs'],'check':({'actor':users.get(checks[v.id].actor_id),'created_at':checks[v.id].created_at,'reason':checks[v.id].content['request']['reason']} if v.id in checks else None)} for v in versions]}


def overview(db,organization_id=None,account_id=None,date_from=None,date_to=None,filter_='all',offset=0,limit=50):
    if organization_id and not db.get(Organization,organization_id): fail('Организация не найдена',404)
    if account_id:
        account=db.get(BankAccount,account_id)
        if not account: fail('Счёт не найден',404)
        if organization_id and account.organization_id!=organization_id: fail('Счёт относится к другой организации')
    query=db.query(MoneyMovement).options(selectinload(MoneyMovement.documents),selectinload(MoneyMovement.counterparty),selectinload(MoneyMovement.account))
    if organization_id is not None: query=query.join(BankAccount,MoneyMovement.account_id==BankAccount.id).filter(BankAccount.organization_id==organization_id)
    if account_id is not None: query=query.filter(MoneyMovement.account_id==account_id)
    from sqlalchemy import func
    effective=func.coalesce(MoneyMovement.doc_date,MoneyMovement.posted_at,MoneyMovement.created_at)
    if date_from: query=query.filter(effective>=datetime.combine(date_from,datetime.min.time(),tzinfo=timezone.utc))
    if date_to:
        from datetime import timedelta
        query=query.filter(effective<datetime.combine(date_to+timedelta(days=1),datetime.min.time(),tzinfo=timezone.utc))
    rows=query.order_by(effective.desc(),MoneyMovement.id.desc()).all()
    versions={}
    for v in db.query(Version).filter(Version.movement_id.in_([r.id for r in rows])).order_by(Version.version): versions[v.movement_id]=v
    checks={c.version_id:c for c in db.query(Check).filter(Check.version_id.in_([v.id for v in versions.values()])).all()}
    attached={r.id:{f.id:f for f in r.documents if f.purpose==FilePurpose.MONEY_MOVEMENT_DOCUMENT} for r in rows}
    for doc,asset in db.query(Document,FileAsset).join(FileAsset,Document.file_id==FileAsset.id).filter(Document.movement_id.in_(attached)):
        if asset.purpose==FilePurpose.MONEY_MOVEMENT_DOCUMENT: attached[doc.movement_id][asset.id]=asset
    person=responsible(db,'accounting')
    totals=defaultdict(lambda:{'income':Decimal(0),'expense':Decimal(0),'unverified_expense':Decimal(0)})
    counts=Counter();queue=[];cache={}
    for row in rows:
        currency=totals[row.currency]
        if row.status==MoneyMovementStatus.POSTED: currency[row.direction.value]+=decimal(row.amount)
        if row.direction!=MoneyDirection.EXPENSE or row.status==MoneyMovementStatus.CANCELLED: continue
        version=versions.get(row.id)
        review=state(db,row,version,checks.get(version.id) if version else None,cache,True,attached[row.id],person)
        counts[review['state']]+=1
        if row.status in (MoneyMovementStatus.DRAFT,MoneyMovementStatus.APPROVED): counts['to_pay']+=1
        if row.status==MoneyMovementStatus.POSTED and review['state']!='verified': currency['unverified_expense']+=decimal(row.amount)
        if filter_=='to_pay': include=row.status in (MoneyMovementStatus.DRAFT,MoneyMovementStatus.APPROVED)
        elif filter_=='attention': include=review['state']!='verified'
        else: include=filter_=='all' or review['state']==filter_
        if include: queue.append({'id':row.id,'date':row.doc_date or row.posted_at or row.created_at,'amount':str(decimal(row.amount)),
            'currency':row.currency,'status':row.status.value,'purpose':row.payment_purpose or row.external_number or 'Назначение не указано',
            'counterparty':row.counterparty.name if row.counterparty else None,'account':row.account.name if row.account else None,'review':review})
    return {'as_of':datetime.now(timezone.utc),'scope':{'organization_id':organization_id,'account_id':account_id,'date_from':date_from,'date_to':date_to},
            'counts':{k:counts[k] for k in ('no_document','unclassified','pending','stale','verified','to_pay')},
            'totals':[{ 'currency':c,**{k:str(v) for k,v in t.items()},'net':str(t['income']-t['expense'])} for c,t in sorted(totals.items())],
            'items':queue[offset:offset+limit],'total':len(queue),'offset':offset,'limit':limit,
            'responsible':{'id':person.id,'name':person.full_name} if person else None,
            'bank_reconciled':False,'actual_profit_available':False}
