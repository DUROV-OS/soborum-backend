"""Named, versioned approvals. Evidence is checked by a person, never inferred from a label."""
from copy import deepcopy
from decimal import Decimal as D, ROUND_CEILING
from app.common.files import FileAsset, file_integrity
from app.costing import service as s
from app.costing.governance_models import CostingApprovalPolicy as Policy, CostingSignoff as Signoff, CostingOverheadPlan as Plan, CostingEvidenceDocument as Document
from app.costing.models import CostingRevision
from app.costing.schemas import EstimateInput, Expenses
from app.costing.technical_basis import fingerprint, item_basis, prepare
from app.users.models import User


def policy(db):
    return db.query(Policy).filter_by(id=1).populate_existing().first()


def responsible(db,stage):
    row=policy(db)
    uid=getattr(row,stage+'_user_id',None)
    user=db.get(User,uid) if uid else None
    return user if user and user.is_active else None


def require_person(db,user,stage):
    assigned=responsible(db,stage)
    if not assigned: s.fail('Не назначен действующий ответственный за этап '+stage,409)
    if user.id!=assigned.id: s.fail('Этот этап лично согласует '+assigned.full_name+'. Права администратора не заменяют согласование.',403)


def may_read(db,user):
    row=policy(db)
    return bool(s.cost_access(user) or row and user.id in (row.production_user_id,row.accounting_user_id,row.director_user_id))


def financial_access(db,user):
    return s.cost_access(user) or any(getattr(responsible(db,stage),'id',None)==user.id for stage in ('accounting','director'))


def lock_sources(db,snapshot):
    """Keep external price/recipe rows stable through validation and commit."""
    from app.warehouse.models import WarehouseMaterial
    from app.moysklad.recipes import Recipe
    mids=sorted({m['warehouse_material_id'] for m in snapshot['sources']['materials'].values() if m.get('warehouse_material_id')})
    rids=sorted({i['content']['source_recipe_id'] for i in snapshot['sources']['items'].values() if i['content'].get('source_recipe_id')})
    for model,ids in ((WarehouseMaterial,mids),(Recipe,rids)):
        if ids: db.query(model).filter(model.id.in_(ids)).order_by(model.id).with_for_update(read=True).populate_existing().all()


def recipe_problems(db,source):
    from app.moysklad.recipes import Recipe
    issues=[]
    for item in source['items'].values():
        data=item['content']
        if not data.get('source_recipe_id'): continue
        recipe=db.get(Recipe,data['source_recipe_id'])
        if not recipe or recipe.archived or recipe.version!=data.get('source_recipe_version'):
            issues.append('Исходная техкарта изменилась: '+item['title']+'. Обновите состав из техкарты.')
    return issues


def latest_signoff(db,target,target_id,stage,digest,version=None):
    actor=responsible(db,stage)
    if not actor: return None
    q=db.query(Signoff).filter_by(target=target,target_id=target_id,stage=stage,fingerprint=digest,actor_id=actor.id)
    if version is not None: q=q.filter_by(version=version)
    return q.order_by(Signoff.id.desc()).first()


def item_digest(item,source):
    basis=item_basis(item,source)
    if item['kind']=='package':
        basis['members']=[item_basis(source['items'][str(row['addon_id'])],source) for row in item['content']['addons']]
    return fingerprint(basis)


def item_ready(db,item,source):
    used={str(item['id']):item}
    if item['kind']=='package': used.update({str(r['addon_id']):source['items'][str(r['addon_id'])] for r in item['content']['addons']})
    return not recipe_problems(db,{'items':used}) and latest_signoff(db,'item',item['id'],'production',item_digest(item,source)) is not None


def populate_item_approvals(db,source):
    source['production_approved']={key:item_ready(db,item,source) for key,item in source['items'].items()}
    return source


def assert_item_price(db,item):
    source=s.sources(db)
    if not item_ready(db,item,source): s.fail('До формирования цены состав должен согласовать начальник производства',409)


def plan_facts(plan):
    labels={'planned_units':'Плановое число изделий','factory':'Постоянные расходы на цех','warehouse':'Постоянные расходы на склад','staff':'Штатные сотрудники','other':'Прочие постоянные расходы'}
    return [{'key':key,'label':label,'value':str(plan.content[key]),'unit':'шт' if key=='planned_units' else '₽ за месяц'} for key,label in labels.items()]


def document_proofs(db,target,target_id,facts,evidence):
    required={f['key']:f for f in facts}
    supplied={e.key:e for e in evidence}
    if len(supplied)!=len(evidence) or set(supplied)!=set(required):
        s.fail('Каждое исходное число должно иметь отдельную ссылку на документ и строку. Проверьте список оснований.')
    proofs=[]
    for key,fact in required.items():
        e=supplied[key]
        if fact['value'] is None or D(e.value)!=D(fact['value']): s.fail('Сумма или ставка изменилась: '+fact['label'],409)
        document=db.get(Document,e.document_id)
        if not document or document.target!=target or document.target_id!=target_id: s.fail('Документ не относится к этому расчёту',409)
        asset=db.get(FileAsset,document.file_id)
        if not asset or file_integrity(asset)!='matches': s.fail('Файл отсутствует, изменён или не имеет исходной контрольной суммы',409)
        proofs.append({'key':key,'value':str(e.value),'document_id':document.id,'file_id':asset.id,
                       'sha256':asset.sha256,'size_bytes':asset.size_bytes,'locator':e.locator,'title':document.title})
    return proofs


def proofs_intact(db,signoff):
    if not signoff: return False
    checked=set()
    for proof in signoff.content.get('evidence',[]):
        key=(proof['file_id'],proof['sha256'],proof['size_bytes'])
        if key in checked: continue
        asset=db.get(FileAsset,proof['file_id'])
        if not asset or asset.sha256!=proof['sha256'] or asset.size_bytes!=proof['size_bytes'] or file_integrity(asset)!='matches': return False
        checked.add(key)
    return bool(signoff.content.get('evidence'))


def plan_state(db,plan):
    digest=fingerprint(plan.content)
    newest=db.query(Plan).filter_by(month=plan.month).order_by(Plan.version.desc()).first()
    accounting=latest_signoff(db,'month',plan.id,'accounting',digest,plan.version)
    director=latest_signoff(db,'month',plan.id,'director',digest,plan.version)
    approved=bool(newest.id==plan.id and accounting and director and proofs_intact(db,accounting)
                  and director.content.get('accounting_signoff_id')==accounting.id)
    total=sum((D(str(plan.content[k])) for k in ('factory','warehouse','staff','other')),D(0))
    return {'id':plan.id,'month':plan.month.isoformat(),'version':plan.version,'content':plan.content,
            'approved':approved,'accounting_approved':bool(accounting and proofs_intact(db,accounting)),
            'director_approved':bool(director),'current':newest.id==plan.id,'total':s.money(total),
            'per_unit':str((total/D(plan.content['planned_units'])).quantize(D('.00000001'),rounding=ROUND_CEILING)),'fingerprint':digest}


def build_snapshot(db,payload,previous=None,refresh=False,estimate_id=None):
    technical=prepare(payload,s.sources(db,previous.get('sources') if previous and not refresh else None))
    approval=latest_signoff(db,'estimate',estimate_id,'production',technical['technical_fingerprint']) if estimate_id else None
    if not approval: return technical
    template=technical['sources']['items'][str(payload.template_id)]
    expenses=deepcopy(payload.expenses.model_dump(mode='json') if payload.expenses else template['content']['expenses'])
    plan=db.get(Plan,payload.overhead_plan_id) if payload.overhead_plan_id else None
    overhead=plan_state(db,plan) if plan else None
    expenses['overhead']=overhead['per_unit'] if overhead and overhead['approved'] else None
    # Allocation is a derived value; a manually entered overhead total never substitutes for an approved month.
    adjusted=payload.model_copy(update={'expenses':Expenses.model_validate(expenses)})
    priced=s.calculate_snapshot(db,adjusted,{'sources':technical['sources']})
    priced['input']=payload.model_dump(mode='json')
    priced.update(technical_basis=technical['technical_basis'],technical_fingerprint=technical['technical_fingerprint'],
                  production_signoff_id=approval.id,overhead_plan=overhead)
    if not overhead or not overhead['approved']:
        priced['result']['missing'].append('Не выбран утверждённый месячный план расходов на цех, склад и штат')
    for item in priced['sources']['items'].values():
        if item['kind']=='addon' and D(str(item['content']['expenses'].get('overhead') or 0))!=0:
            priced['result']['missing'].append('Накладные опции '+item['title']+' нужно включить в единый месячный план, чтобы избежать двойного счёта')
    if priced['result']['missing']:
        priced['result']['status']='incomplete'
        for key in ('cost','profit','margin_percent','minimum_price','base_cost','rate_cost'): priced['result'][key]=None
    return priced


def financial_facts(snapshot):
    facts=[]
    # Proposed commercial terms are input figures too; a label or typed price is not evidence.
    for key,label,unit in (('sale_price','Договорная цена всего проекта','₽'),('discount','Скидка от прайса','%'),('discount_amount','Скидка в рублях','₽')):
        value=snapshot['input'].get(key)
        if value is not None: facts.append({'key':'sale:'+key,'label':label,'value':str(value),'unit':unit})
    for i,line in enumerate(snapshot['commercial']):
        facts.append({'key':f'price:{i}:{line["item_id"]}','label':'Прайсовая цена · '+line['title'], 'value':line['unit_price'],'unit':'₽ на изделие'})
    for i,line in enumerate(snapshot['bom']):
        facts.append({'key':f'material:{i}:{line["material_id"]}','label':line['name'],'value':line['price'],'unit':'₽ / '+line['unit']})
    used={str(x['item_id']) for x in snapshot['commercial']}
    base=str(snapshot['input']['template_id'])
    for key in sorted(used):
        item=snapshot['sources']['items'][key]
        expenses=snapshot['expenses'] if key==base else item['content']['expenses']
        profile=snapshot['profile'] if key==base else s.profile_for(item,snapshot['sources']['settings'])
        for field,label in s.EXPENSE_LABELS.items():
            if field=='overhead' and key==base: continue
            facts.append({'key':f'expense:{key}:{field}','label':item['title']+' · '+label,'value':expenses.get(field),'unit':'₽ на изделие'})
        for field,label in (('labor','Сдельные работы от выручки'),('management','Вознаграждение продавца'),('realtor','Комиссия риелтора'),('tax','Налоги от выручки')):
            facts.append({'key':f'rate:{key}:{field}','label':item['title']+' · '+label,'value':str(profile.get(field,0)),'unit':'% выручки'})
    return facts


def source_problems(db,snapshot,current=None):
    problems=recipe_problems(db,snapshot['sources'])
    current=current or s.sources(db)
    payload=EstimateInput.model_validate(snapshot['input'])
    try:
        if prepare(payload,current)['technical_fingerprint']!=snapshot.get('technical_fingerprint'):
            problems.append('Состав или исходная техкарта изменились; актуализируйте расчёт и получите новое согласование производства')
    except Exception as error:
        from fastapi import HTTPException
        if not isinstance(error,HTTPException): raise
        problems.append('Исходный состав больше недоступен')
    for mid,old in snapshot['sources']['materials'].items():
        new=current['materials'].get(mid)
        if not new or any(new.get(k)!=old.get(k) for k in ('price','version','unit','effective_on','archived')):
            problems.append('Изменилось основание цены материала: '+old['name'])
    plan=snapshot.get('overhead_plan')
    if not plan or not db.get(Plan,plan['id']) or not plan_state(db,db.get(Plan,plan['id']))['approved']:
        problems.append('Нет действующего утверждённого месячного плана расходов')
    return problems


def review_state(db,row,revision=None,current_sources=None):
    revision=revision or s.get_revision(db,row)
    snap=revision.snapshot
    digest=fingerprint(snap)
    production=latest_signoff(db,'estimate',row.id,'production',snap.get('technical_fingerprint',''))
    accounting=latest_signoff(db,'estimate',row.id,'accounting',digest,revision.number)
    director=latest_signoff(db,'estimate',row.id,'director',digest,revision.number)
    issues=list(snap['result']['missing'])
    if not production: issues.insert(0,'Начальник производства ещё не согласовал состав и работы')
    if production: issues+=source_problems(db,snap,current_sources)
    intact=proofs_intact(db,accounting) if accounting else False
    accounting_valid=bool(accounting and intact and not issues)
    if accounting and not intact: issues.append('Документ финансового согласования недоступен или изменён')
    if not accounting_valid: issues.append('Бухгалтер должен проверить каждую сумму, ставку и документ')
    if snap['result']['status']=='below_target': issues.append('Маржа ниже обязательных 35%')
    approved=bool(row.status=='approved' and production and accounting_valid and director
        and director.content.get('accounting_signoff_id')==accounting.id and snap['result']['status']=='meets_target')
    return {'production_approved':bool(production),'accounting_approved':accounting_valid,'director_approved':approved,
        'production_signoff_id':production.id if production else None,'accounting_signoff_id':accounting.id if accounting else None,
        'director_signoff_id':director.id if director else None,'issues':list(dict.fromkeys(issues)),
        'next_stage':'production' if not production else 'accounting' if not accounting_valid else 'director' if not approved else 'complete'}


def assert_approved(db,row):
    lock_sources(db,s.get_revision(db,row).snapshot)
    state=review_state(db,row)
    if not state['director_approved']:
        s.fail('Нет действующего полного согласования расчёта: '+'; '.join(state['issues'] or ['руководитель должен утвердить цену']),409)


def replay(db,user,target,target_id,stage,body):
    row=db.query(Signoff).filter_by(request_id=str(body.request_id)).first()
    if row and (row.actor_id!=user.id or row.target!=target or row.target_id!=target_id or row.stage!=stage or row.content.get('request')!=body.model_dump(mode='json')):
        s.fail('Идентификатор запроса уже использован для другого согласования',409)
    return row


def record(db,user,target,target_id,stage,version,digest,body,revision_id=None,**content):
    row=Signoff(target=target,target_id=target_id,stage=stage,version=version,fingerprint=digest,
        revision_id=revision_id,actor_id=user.id,request_id=str(body.request_id),
        content={'request':body.model_dump(mode='json'),**content})
    db.add(row);db.flush();return row


def approve_production(db,user,row,body):
    require_person(db,user,'production');s.lock(db)
    if replay(db,user,'estimate',row.id,'production',body): return
    if row.archived or row.version!=body.version: s.fail('Версия расчёта изменилась',409)
    if len(body.work_scope.strip())<5: s.fail('Подтвердите состав работ и технологические условия')
    revision=s.get_revision(db,row)
    lock_sources(db,revision.snapshot)
    problems=recipe_problems(db,revision.snapshot['sources'])
    if problems: s.fail('; '.join(problems),409)
    payload=EstimateInput.model_validate(revision.snapshot['input'])
    technical=prepare(payload,s.sources(db,revision.snapshot['sources']))
    live=prepare(payload,s.sources(db))
    if technical['technical_fingerprint']!=live['technical_fingerprint']: s.fail('Состав источника изменился. Сначала актуализируйте расчёт.',409)
    signoff=record(db,user,'estimate',row.id,'production',row.version,technical['technical_fingerprint'],body,revision.id,
                  technical_basis=technical['technical_basis'],work_scope=body.work_scope)
    # Preserve the submitted technical draft. Calculation is a new derived revision.
    snapshot=build_snapshot(db,payload,revision.snapshot,estimate_id=row.id)
    row.version+=1;row.status='draft'
    db.add(CostingRevision(estimate_id=row.id,number=row.version,actor_id=user.id,
        reason='Расчёт после согласования состава начальником производства',request_id=str(body.request_id),
        request_data={'production_signoff_id':signoff.id},snapshot=snapshot))
    db.commit()


def approve_accounting(db,user,row,body):
    require_person(db,user,'accounting');s.lock(db)
    if replay(db,user,'estimate',row.id,'accounting',body): return
    if row.archived or row.version!=body.version: s.fail('Версия расчёта изменилась',409)
    revision=s.get_revision(db,row);snap=revision.snapshot
    lock_sources(db,snap)
    if not latest_signoff(db,'estimate',row.id,'production',snap.get('technical_fingerprint','')):
        s.fail('Сначала требуется личное согласование начальника производства',409)
    issues=list(snap['result']['missing'])+source_problems(db,snap)
    if issues: s.fail('Нельзя подтвердить неполный расчёт: '+'; '.join(issues),409)
    evidence=document_proofs(db,'estimate',row.id,financial_facts(snap),body.evidence)
    record(db,user,'estimate',row.id,'accounting',row.version,fingerprint(snap),body,revision.id,evidence=evidence)
    db.commit()


def approve_director(db,user,row,body):
    require_person(db,user,'director');s.lock(db)
    if replay(db,user,'estimate',row.id,'director',body): return
    if row.archived or row.version!=body.version: s.fail('Версия расчёта изменилась',409)
    revision=s.get_revision(db,row);snap=revision.snapshot
    lock_sources(db,snap)
    state=review_state(db,row,revision)
    if not state['accounting_approved']: s.fail('Сначала нужны действующие согласования производства и бухгалтера: '+'; '.join(state['issues']),409)
    if snap['result']['status']!='meets_target': s.fail('Для утверждения обязательна полная себестоимость и маржа не ниже 35%')
    record(db,user,'estimate',row.id,'director',row.version,fingerprint(snap),body,revision.id,accounting_signoff_id=state['accounting_signoff_id'])
    row.status='approved';db.commit()
