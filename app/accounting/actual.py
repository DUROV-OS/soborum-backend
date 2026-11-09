"""Documented incurred costs, separate from payments and planned selling prices."""
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal as D, ROUND_HALF_UP, ROUND_DOWN
from zoneinfo import ZoneInfo
from fastapi import HTTPException
from app.accounting.actual_models import ActualCostRecord as Record, ActualCostVersion as Version, ActualCostDecision as Decision, ActualCostDocument as Document
from app.accounting.actual_schemas import CostInput, CATEGORIES, FIXED
from app.accounting.evidence import canonical, checked_file
from app.accounting.models import Organization, SupplierOrder
from app.common.files import FileAsset
from app.costing import governance as g
from app.costing.models import CostingEstimate, CostingRevision
from app.costing.governance_models import CostingOverheadPlan
from app.costing.service import money
from app.purchasing.models import PurchaseReceipt
from app.purchasing.returns import returned_lines
from app.sales.models import SalesOrder
from app.warehouse.models import StockMovement, StockMovementReason
from app.warehouse.service import productions_of_movements
from app.warehouse.controls import returned_quantity, return_facts
from app.clients.models import Client


def fail(text, status=422):
    raise HTTPException(status, text)


def normalized(text):
    return ' '.join(text.casefold().split())


def identity(body):
    if body.kind=='material':
        return canonical(['material',body.receipt_id,body.receipt_line,body.movement_id])
    return canonical(['document',body.organization_id,normalized(body.issuer),normalized(body.number),str(body.document_date),normalized(body.line_ref)])


def heads(db):
    result={}
    for v in db.query(Version).order_by(Version.id): result[v.record_id]=v
    return list(result.values())


def decisions(db,version):
    return {d.action:d for d in db.query(Decision).filter_by(version_id=version.id)}


def reservations(db,exclude=None):
    return [v for v in heads(db) if v.record_id!=exclude and 'void' not in decisions(db,v)]


def local_date(value):
    return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value).astimezone(ZoneInfo('Europe/Moscow')).date()


def utc(value):
    return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value).astimezone(timezone.utc)


def material_value(db,receipt_id,line_index,quantity,record_id=None):
    receipt=db.get(PurchaseReceipt,receipt_id)
    if not receipt or receipt.status!='posted' or line_index>=len(receipt.lines): fail('Строка приёмки не найдена')
    line=receipt.lines[line_index];price=D(str(line['unit_price']))
    prior=D(0)
    for version in reservations(db,record_id):
        r=version.content['request']
        if r['kind']=='material' and r['receipt_id']==receipt_id and r['receipt_line']==line_index and (record_id is None or version.record_id<record_id):
            prior+=D(r['quantity'])
    # Cumulative rounding allocates every kopeck once, even when a lot is issued in tiny parts.
    value=((prior+quantity)*price).quantize(D('.01'),rounding=ROUND_HALF_UP)-(prior*price).quantize(D('.01'),rounding=ROUND_HALF_UP)
    return {'amount':money(value),'unit_price':str(price),'previous_quantity':str(prior),'quantity':str(quantity)}


def source(db,body,exclude=None,locking=False):
    if not db.get(Organization,body.organization_id): fail('Организация не найдена')
    if body.incurred_on>datetime.now(ZoneInfo('Europe/Moscow')).date() or body.document_date>datetime.now(ZoneInfo('Europe/Moscow')).date():
        fail('Будущая дата не подтверждает фактически понесённый расход')
    document=db.query(Document).filter_by(file_id=body.evidence.file_id).first()
    asset=db.get(FileAsset,body.evidence.file_id) if document else None
    if not asset or checked_file(asset)!='matches': fail('Загрузите документ затрат с подтверждённой целостностью файла')
    proof={'file_id':asset.id,'sha256':asset.sha256,'size_bytes':asset.size_bytes,'filename':asset.filename}
    projects={}
    for a in body.allocations:
        if a.project_id:
            project=db.get(CostingEstimate,a.project_id)
            if not project or project.archived: fail('Выберите действующий проект / смету')
            order=db.get(SalesOrder,project.sales_order_id) if project.sales_order_id else None
            if order and (order.organization_id!=body.organization_id or order.currency!='RUB'):
                fail('Организация и валюта затрат должны совпадать с заказом проекта')
            projects[str(project.id)]={'id':project.id,'client_id':project.client_id,'sales_order_id':project.sales_order_id,
                                      'organization_id':order.organization_id if order else None,'currency':order.currency if order else 'RUB'}
        if a.month and a.month>body.incurred_on.replace(day=1): fail('Нельзя признать затраты будущего месяца')
    result={'proof':proof,'projects':projects}
    other=reservations(db,exclude)
    if body.kind=='document':
        for v in other:
            request=v.content['request']
            if request['kind']=='document' and v.content['source']['proof']['sha256']==asset.sha256 and normalized(request['line_ref'])==normalized(body.line_ref):
                fail('Эта строка файла уже зарегистрирована. Исправьте существующую запись.',409)
        return result
    query=db.query(PurchaseReceipt).filter_by(id=body.receipt_id)
    receipt=(query.with_for_update().populate_existing() if locking else query).first()
    movement=db.get(StockMovement,body.movement_id)
    if not receipt or receipt.status!='posted' or not receipt.operation_id or body.receipt_line>=len(receipt.lines):
        fail('Нужна проведённая приёмка с реальным движением склада')
    line=receipt.lines[body.receipt_line]
    allowed={StockMovementReason.ISSUED,StockMovementReason.ISSUED_TECHCARD,StockMovementReason.ISSUED_MANUAL}
    if not movement or movement.reason not in allowed or D(str(movement.delta))>=0:
        fail('Выберите фактическую выдачу материала в производство')
    if not line.get('stock_tracked') or line.get('material_id')!=movement.warehouse_material_id:
        fail('Материал поступления и выдачи должен совпадать')
    order=db.get(SupplierOrder,receipt.order_id) if receipt.order_id else None
    if order and order.currency!='RUB': fail('Для валютной партии нужен отдельный документ пересчёта в рубли')
    received_movements=db.query(StockMovement).filter_by(operation_id=receipt.operation_id,warehouse_material_id=movement.warehouse_material_id,reason=StockMovementReason.RECEIPT).all()
    receipt_quantity=sum((D(str(l['quantity'])) for l in receipt.lines if l.get('material_id')==movement.warehouse_material_id and l.get('stock_tracked')),D(0))
    if sum((D(str(m.delta)) for m in received_movements),D(0))!=receipt_quantity:
        fail('Количество приёмки не совпадает с движениями склада')
    production=productions_of_movements(db,[movement]).get(movement.id)
    project=db.get(CostingEstimate,body.allocations[0].project_id)
    client=db.get(Client,project.client_id) if project.client_id else None
    if not production or not client or client.cycle_id!=production.cycle_id:
        fail('Выдача должна относиться к производству клиента выбранной сметы')
    # An individual lot is selected explicitly. No undocumented FIFO/average-price policy is invented.
    received=D(str(line['quantity']));returned=returned_lines(db,receipt.id)[body.receipt_line]
    issued=-D(str(movement.delta))-returned_quantity(db,movement.id)
    if any(not row['intact'] for row in return_facts(db,movement.id)): fail('Документ возврата отсутствует или изменён. Требуется сверка склада.',409)
    used_receipt=used_movement=D(0)
    for v in other:
        r=v.content['request']
        if r['kind']!='material': continue
        if v.content['source'].get('material',{}).get('production_id')==production.id and r['allocations'][0]['project_id']!=project.id:
            fail('Это производство уже связано с другой сметой затрат; сначала исправьте прежнее распределение',409)
        if r['receipt_id']==receipt.id and r['receipt_line']==body.receipt_line: used_receipt+=D(r['quantity'])
        if r['movement_id']==movement.id: used_movement+=D(r['quantity'])
    if used_receipt+body.quantity>received-returned: fail('Признанное количество превышает партию за вычетом возвратов',409)
    if used_movement+body.quantity>issued: fail('Признанное количество превышает фактическую выдачу',409)
    issued_at=movement.operation.occurred_at if movement.operation else movement.created_at
    if utc(receipt.occurred_at)>utc(issued_at):
        fail('Поступление не может быть позже выдачи')
    if body.incurred_on!=local_date(issued_at): fail('Дата затрат на материалы должна совпадать с датой выдачи')
    valuation=material_value(db,receipt.id,body.receipt_line,body.quantity,exclude)
    expected=D(valuation['amount'])
    if expected<=0 or body.gross_amount!=expected: fail('Сумма материалов по партии с учётом округления частичных выдач: '+money(expected)+' ₽')
    result['material']={'receipt_id':receipt.id,'receipt_line':body.receipt_line,'line':line,'supplier_id':receipt.supplier_id,
        'occurred_at':utc(receipt.occurred_at).isoformat(),'returned':str(returned.normalize()),'movement_id':movement.id,'delta':str(D(str(movement.delta)).normalize()),
        'production_returns':return_facts(db,movement.id),'reason':movement.reason.value,'operation_id':movement.operation_id,'receipt_operation_id':receipt.operation_id,
        'inbound_movements':[{'id':m.id,'delta':str(D(str(m.delta)).normalize())} for m in sorted(received_movements,key=lambda m:m.id)],
        'currency':order.currency if order else 'RUB','valuation':valuation,'production_id':production.id,'issued_on':str(local_date(issued_at))}
    return result


def state(db,version):
    d=decisions(db,version)
    if 'void' in d: return {'state':'void','issues':[]}
    try:
        current=source(db,CostInput.model_validate(version.content['request']),version.record_id)
        if canonical(current)!=canonical(version.content['source']): return {'state':'stale','issues':['Изменилось документальное или складское основание']}
    except (HTTPException,ValueError) as e:
        return {'state':'stale','issues':[str(e.detail) if isinstance(e,HTTPException) else 'Неверное сохранённое основание']}
    check=d.get('recognize')
    person=g.responsible(db,'accounting')
    if check:
        if not person or check.actor_id!=person.id or check.content['source_hash']!=canonical(version.content):
            return {'state':'stale','issues':['Требуется повторное личное подтверждение назначенного бухгалтера']}
        return {'state':'recognized','issues':[],'checked_by':person.full_name,'checked_at':check.created_at}
    return {'state':'pending','issues':['Ожидает личного признания затрат бухгалтером']}


def save(db,body,user,record_id=None):
    data=body.model_dump(mode='json')
    replay=db.query(Version).filter_by(request_id=str(body.request_id)).first()
    if replay:
        if replay.actor_id!=user.id or replay.content['request']!=data or record_id not in (None,replay.record_id): fail('Повторный запрос отличается',409)
        return replay
    key=identity(body)
    record=db.get(Record,record_id) if record_id else db.query(Record).filter_by(identity=key).first()
    if record_id and not record: fail('Запись затрат не найдена',404)
    current=db.query(Version).filter_by(record_id=record.id).order_by(Version.version.desc()).first() if record else None
    if record and record.identity!=key: fail('Реквизиты первичного основания нельзя подменить. Отмените признание и заведите отдельное основание.',409)
    if body.expected_version!=(current.version if current else 0): fail('Основание уже существует или версия изменилась. Откройте запись.',409)
    if current:
        d=decisions(db,current)
        if 'recognize' in d and 'void' not in d: fail('Сначала отмените прежнее признание с объяснением; история сохранится',409)
    snapshot=source(db,body,record.id if record else None,True)
    if not record:
        record=Record(identity=key,kind=body.kind,actor_id=user.id);db.add(record);db.flush()
    version=Version(record_id=record.id,version=body.expected_version+1,actor_id=user.id,request_id=str(body.request_id),
                    content={'request':data,'source':snapshot,'net_amount':money(body.gross_amount-body.excluded_tax)})
    db.add(version);db.flush();return version


def decide(db,record_id,body,user):
    g.require_person(db,user,'accounting')
    version=db.query(Version).filter_by(record_id=record_id).order_by(Version.version.desc()).first()
    if not version: fail('Запись затрат не найдена',404)
    if version.version!=body.version or canonical(version.content)!=body.source_hash: fail('Версия изменилась; обновите карточку',409)
    replay=db.query(Decision).filter_by(request_id=str(body.request_id)).first()
    data=body.model_dump(mode='json')
    if replay:
        if replay.actor_id!=user.id or replay.version_id!=version.id or replay.content['request']!=data: fail('Повторный запрос отличается',409)
        return replay
    existing=decisions(db,version)
    if body.action in existing or 'void' in existing: fail('Решение уже принято; обновите карточку',409)
    if body.action=='recognize':
        current=source(db,CostInput.model_validate(version.content['request']),record_id,True)
        if canonical(current)!=canonical(version.content['source']): fail('Основание изменилось; создайте новую версию',409)
    decision=Decision(version_id=version.id,action=body.action,actor_id=user.id,request_id=str(body.request_id),
                      content={'request':data,'source_hash':body.source_hash})
    db.add(decision);db.flush();return decision


def row_out(db,v):
    return {'id':v.record_id,'version':v.version,'source_hash':canonical(v.content),'data':v.content['request'],
            'net_amount':v.content['net_amount'],'proof':v.content['source']['proof'],'source':v.content['source'],
            'review':state(db,v),'created_at':v.created_at}


def detail(db,id):
    versions=db.query(Version).filter_by(record_id=id).order_by(Version.version.desc()).all()
    if not versions: fail('Запись затрат не найдена',404)
    result=row_out(db,versions[0])
    result['history']=[{'version':v.version,'created_at':v.created_at,'reason':v.content['request']['reason'],
        'decisions':[{'action':d.action,'actor_id':d.actor_id,'created_at':d.created_at,'reason':d.content['request']['reason']} for d in decisions(db,v).values()]} for v in versions]
    return result


def allocate_month(totals,weights,planned_units):
    """Allocate kopecks without creating or losing money, retaining the unused plan share."""
    weights={**weights,None:D(planned_units)-sum(weights.values(),D(0))}
    allocated={id:{} for id in weights}
    for category in sorted(FIXED):
        total=totals.get(category,D(0))
        exact={id:total*units/D(planned_units) for id,units in weights.items()}
        rounded={id:value.quantize(D('.01'),rounding=ROUND_DOWN) for id,value in exact.items()}
        cents=int((total-sum(rounded.values(),D(0)))*100)
        order=sorted(weights,key=lambda id:(-(exact[id]-rounded[id]),id if id is not None else 10**20))
        for id in order[:cents]: rounded[id]+=D('.01')
        for id in weights: allocated[id][category]=rounded[id]
    return allocated


def overview(db,project_id=None,with_closures=True):
    versions=heads(db);rows=[row_out(db,v) for v in versions]
    projects=db.query(CostingEstimate).filter_by(archived=False).order_by(CostingEstimate.id.desc()).all()
    revisions={}
    for revision in db.query(CostingRevision).order_by(CostingRevision.id): revisions[revision.estimate_id]=revision
    plans={p.id:p for p in db.query(CostingOverheadPlan)}
    used_units=defaultdict(D);plan_units=defaultdict(dict)
    for p in projects:
        rev=revisions.get(p.id)
        if rev and rev.request_data.get('overhead_plan_id'):
            used_units[rev.request_data['overhead_plan_id']]+=D(str(rev.request_data.get('quantity',1)))
            plan_units[rev.request_data['overhead_plan_id']][p.id]=D(str(rev.request_data.get('quantity',1)))
    results=[]
    for project in projects:
        if project_id is not None and project.id!=project_id: continue
        own=[r for r in rows if any(a['project_id']==project.id for a in r['data']['allocations'])]
        sums=defaultdict(D);issues=[]
        for row in own:
            if row['review']['state']=='recognized':
                for a in row['data']['allocations']:
                    if a['project_id']==project.id: sums[a['category']]+=D(a['amount'])
            elif row['review']['state']!='void': issues.append('Затрата №'+str(row['id'])+' не признана: '+'; '.join(row['review']['issues']))
        revision=revisions.get(project.id);plan=None;overhead=None;allocation=None;monthly_rows=[]
        if revision: plan=plans.get(revision.request_data.get('overhead_plan_id'))
        if not plan: issues.append('Не выбран документально утверждённый месячный план постоянных затрат')
        else:
            approved=g.plan_state(db,plan)
            monthly=defaultdict(D);month=str(plan.month)
            monthly_rows=[r for r in rows if any(a['month']==month for a in r['data']['allocations'])]
            for row in monthly_rows:
                if row['review']['state']=='recognized':
                    for a in row['data']['allocations']:
                        if a['month']==month: monthly[a['category']]+=D(a['amount'])
                elif row['review']['state']!='void': issues.append('Месячная затрата №'+str(row['id'])+' не признана')
            if not approved['approved']: issues.append('Месячный план изменён или ещё не утверждён')
            if used_units[plan.id]>D(plan.content['planned_units']): issues.append('Число изделий в связанных проектах превышает месячный план; пересогласуйте план')
            for key in ('factory','staff'):
                if monthly[key]<=0: issues.append('Нет признанных фактических затрат по статье «'+CATEGORIES[key]+'»')
            units=D(str(revision.request_data.get('quantity',1)))
            if approved['approved'] and used_units[plan.id]<=D(plan.content['planned_units']):
                shares=allocate_month(monthly,plan_units[plan.id],plan.content['planned_units'])
                overhead=sum(shares[project.id].values(),D(0))
                allocation={'month':month,'plan_id':plan.id,'planned_units':plan.content['planned_units'],'project_units':str(units),
                            'recognized_month_total':money(sum(monthly.values(),D(0))),
                            'categories':{k:money(v) for k,v in shares[project.id].items()},
                            'unallocated_month_total':money(sum(shares[None].values(),D(0))),
                            'rounding':'largest_remainder_kopecks_by_project_id',
                            'record_ids':[r['id'] for r in monthly_rows if r['review']['state']=='recognized']}
        direct=sum(sums.values(),D(0))
        subtotal=direct+(overhead or D(0))
        planned=revision.snapshot.get('result',{}) if revision else {}
        result={'id':project.id,'title':project.title,'number':project.number,'client_id':project.client_id,'sales_order_id':project.sales_order_id,
            'recognized_direct':money(direct),'allocated_overhead':money(overhead),'recognized_subtotal':money(subtotal),
            'categories':{k:money(v) for k,v in sums.items()},'overhead':allocation,'issues':list(dict.fromkeys(issues)),
            'planned_cost':planned.get('cost'),'planned_price':planned.get('price'),
            'final_cost':None,'actual_profit':None,'actual_margin_percent':None,'complete':False,'record_ids':[r['id'] for r in own]}
        result['source_hash']=canonical({'project':{'id':project.id,'status':project.status,'client_id':project.client_id,'sales_order_id':project.sales_order_id,'sales_order_line':project.sales_order_line},
            'revision':revision.id if revision else None,'result':result,'rows':[{ 'id':r['id'],'version':r['version'],'source_hash':r['source_hash'],'state':r['review']['state']} for r in sorted({r['id']:r for r in own+monthly_rows}.values(),key=lambda r:r['id'])]})
        if with_closures:
            from app.accounting.actual_closure import closure_state
            result.update(closure_state(db,project,result))
        results.append(result)
    if project_id is not None and not results: fail('Проект не найден',404)
    shown=rows if project_id is None else [r for r in rows if any(a['project_id']==project_id for a in r['data']['allocations'])]
    return {'as_of':datetime.now(timezone.utc),'currency':'RUB','projects':results,'records':list(reversed(shown)),
            'counts':{key:sum(r['review']['state']==key for r in shown) for key in ('pending','recognized','stale','void')},
            'cash_added_to_cost':False,'actual_profit_available':any(p['complete'] for p in results)}
