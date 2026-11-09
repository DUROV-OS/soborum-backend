"""Personal close of an evidenced project result. Missing costs never become zero implicitly."""
from decimal import Decimal as D
from app.accounting import actual as a
from app.accounting.actual_models import ActualCostClosure as Closure, ActualCostDocument as Document
from app.accounting.actual_schemas import CATEGORIES, FIXED, ClosureInput
from app.common.files import FileAsset
from app.costing import governance as g
from app.costing.models import CostingEstimate
from app.costing.service import money
from app.production.models import Production
from app.sales.models import SalesOrder, SalesShipment
from app.warehouse.models import StockMovement, StockMovementReason
from app.warehouse.service import productions_of_movements


def evidence(db,citation):
    if not db.query(Document).filter_by(file_id=citation.file_id).first(): a.fail('Загрузите документ в реестр фактических затрат')
    asset=db.get(FileAsset,citation.file_id)
    if not asset or a.checked_file(asset)!='matches': a.fail('Документ закрытия отсутствует или изменён')
    return {'file_id':asset.id,'sha256':asset.sha256,'size_bytes':asset.size_bytes,'locator':citation.locator,'filename':asset.filename}


def closure_basis(db,project,report,body,locking=False):
    if report['issues']: a.fail('Сначала устраните препятствия: '+'; '.join(report['issues']),409)
    if project.status!='approved': a.fail('Смета проекта должна пройти согласование состава, расходов и цены',409)
    amounts={**report['categories'],**(report['overhead'] or {}).get('categories',{})}
    missing={k for k in CATEGORIES if D(amounts.get(k,'0'))==0}
    supplied={r.category for r in body.zero_categories}
    if supplied!=missing or len(supplied)!=len(body.zero_categories):
        a.fail('Каждая отсутствующая статья требует отдельного основания нулевой суммы: '+', '.join(CATEGORIES[k] for k in sorted(missing)))
    proofs=[evidence(db,body.completion_evidence),evidence(db,body.revenue_evidence)]
    proofs.extend(evidence(db,z.evidence) for z in body.zero_categories)
    if len(set(body.production_ids))!=len(body.production_ids) or len(set(body.shipment_ids))!=len(body.shipment_ids): a.fail('Не повторяйте производство или отгрузку')
    order_query=db.query(SalesOrder).filter_by(id=project.sales_order_id)
    order=(order_query.with_for_update().populate_existing() if locking else order_query).first() if project.sales_order_id else None
    index=project.sales_order_line
    if not order or index is None or index>=len(order.lines) or order.archived or order.status=='cancelled' or order.currency!='RUB':
        a.fail('Для фактической выручки свяжите смету с действующей строкой заказа в рублях')
    revenue=quantity=D(0);shipments=[]
    for id in sorted(body.shipment_ids):
        shipment_query=db.query(SalesShipment).filter_by(id=id)
        row=(shipment_query.with_for_update().populate_existing() if locking else shipment_query).first()
        if not row or row.order_id!=order.id or row.status!='posted' or row.archived or row.currency!='RUB': a.fail('Нужна проведённая действующая отгрузка по заказу проекта')
        lines=[l for l in row.lines if l.get('order_line')==index]
        if not lines: a.fail('В отгрузке нет строки заказа этого проекта')
        for line in lines:
            qty=D(str(line['quantity']));total=D(str(line['line_total']))
            if qty<=0 or total<0: a.fail('В отгрузке недопустимые количество или сумма')
            quantity+=qty;revenue+=total
        shipments.append({'id':row.id,'version':row.version,'lines':lines,'number':row.number,'occurred_at':a.utc(row.occurred_at).isoformat()})
    if quantity!=D(str(order.lines[index]['quantity'])): a.fail('Проект закрывается после полной отгрузки согласованной позиции')
    if body.revenue_excluded_tax>=revenue: a.fail('Выручка после исключаемого налога должна быть положительной')
    material_rows=[r for r in a.overview(db,project.id,False)['records'] if r['data']['kind']=='material' and r['review']['state']=='recognized']
    used_productions={r['source']['material']['production_id'] for r in material_rows}
    if not used_productions.issubset(set(body.production_ids)): a.fail('Включите все производства с признанными затратами')
    if project.client_id:
        client=db.get(a.Client,project.client_id)
        existing={p.id for p in db.query(Production).filter_by(cycle_id=client.cycle_id)} if client else set()
        other={v.content['source']['material']['production_id'] for v in a.reservations(db) if v.content['request']['kind']=='material' and v.content['request']['allocations'][0]['project_id']!=project.id}
        if existing-set(body.production_ids)-other: a.fail('В цикле клиента остались производства без распределения по проектам')
        if set(body.production_ids)&other: a.fail('Производство уже относится к другой смете затрат')
    production_sources=[]
    for id in sorted(body.production_ids):
        production=db.get(Production,id)
        if not production or not project.client_id or not production.cycle.client or production.cycle.client.id!=project.client_id: a.fail('Производство не относится к клиенту проекта')
        movements=db.query(StockMovement).filter(StockMovement.reason.in_([StockMovementReason.ISSUED,StockMovementReason.ISSUED_TECHCARD,StockMovementReason.ISSUED_MANUAL]),StockMovement.delta<0).order_by(StockMovement.id).all()
        belong=productions_of_movements(db,movements)
        for movement in movements:
            if movement.id not in belong or belong[movement.id].id!=id: continue
            if any(not row['intact'] for row in a.return_facts(db,movement.id)): a.fail('Документ возврата повреждён. Сначала восстановите подтверждение.',409)
            from app.warehouse.reconciliation import evidence as allocation_evidence
            reconciled = allocation_evidence(db,movement)
            if reconciled and not reconciled['intact']: a.fail('Основание сверки старой выдачи требует проверки',409)
            recognized=sum((D(r['data']['quantity']) for r in material_rows if r['data']['movement_id']==movement.id),D(0))
            if recognized!=-D(str(movement.delta))-a.returned_quantity(db,movement.id): a.fail('Не оценена вся фактическая выдача №'+str(movement.id),409)
            production_sources.append({'production_id':id,'movement_id':movement.id,'quantity':str(recognized),'returns':a.return_facts(db,movement.id),**({'reconciliation':reconciled} if reconciled else {})})
    # Persist declared scope and operational references; the accountant confirms consumption,
    # accepted work, zero categories and completeness against the attached closing document.
    net=revenue-body.revenue_excluded_tax;cost=D(report['recognized_subtotal']);profit=net-cost
    return {'source_hash':report['source_hash'],'proofs':proofs,'shipments':shipments,'productions':production_sources,
            'order_id':order.id,'order_version':order.version,'order_line':index,'gross_revenue':money(revenue),
            'net_revenue':money(net),'final_cost':money(cost),'actual_profit':money(profit),
            'actual_margin_percent':money(profit/net*100),'below_target':profit<net*D('.35')}


def closure_state(db,project,report):
    rows=db.query(Closure).filter_by(project_id=project.id).order_by(Closure.id.desc()).all()
    accounting=next((c for c in rows if c.stage=='accounting'),None)
    if not accounting: return {'closure':{'state':'open','issues':['Сверьте полноту затрат и выручки; затем бухгалтер передаст результат руководителю']}}
    person=g.responsible(db,'accounting')
    try:
        if not person or accounting.actor_id!=person.id or accounting.source_hash!=report['source_hash']: a.fail('Расчёт или ответственный изменились')
        current=closure_basis(db,project,report,ClosureInput.model_validate(accounting.content['request']))
        if a.canonical(current)!=a.canonical(accounting.content['basis']): a.fail('Основания закрытия изменились')
    except (a.HTTPException,ValueError,KeyError) as e:
        return {'closure':{'state':'stale','issues':[str(e.detail) if isinstance(e,a.HTTPException) else 'Основание закрытия не подтверждено']}}
    director=g.responsible(db,'director')
    approved=next((c for c in rows if c.stage=='director' and c.content['accounting_closure_id']==accounting.id and director and c.actor_id==director.id and c.source_hash==report['source_hash']),None)
    result={'closure':{'state':'approved' if approved else 'awaiting_director','accounting_id':accounting.id,'checked_by':person.full_name,
                      'checked_at':accounting.created_at,'director':director.full_name if director else None,'approved_at':approved.created_at if approved else None,
                      'issues':[],'proposed_result':current}}
    if approved: result.update({k:current[k] for k in ('final_cost','actual_profit','actual_margin_percent','net_revenue','below_target')},complete=True)
    return result


def close_accounting(db,id,body,user):
    g.require_person(db,user,'accounting')
    report=a.overview(db,id,False)['projects'][0]
    if body.source_hash!=report['source_hash']: a.fail('Исходные затраты изменились; обновите расчёт',409)
    old=db.query(Closure).filter_by(request_id=str(body.request_id)).first()
    data=body.model_dump(mode='json')
    if old:
        if old.project_id!=id or old.actor_id!=user.id or old.stage!='accounting' or old.content['request']!=data: a.fail('Повторный запрос отличается',409)
        return old
    basis=closure_basis(db,db.get(CostingEstimate,id),report,body,True)
    row=Closure(project_id=id,stage='accounting',source_hash=body.source_hash,actor_id=user.id,request_id=str(body.request_id),content={'request':data,'basis':basis})
    db.add(row);db.flush();return row


def close_director(db,id,body,user):
    g.require_person(db,user,'director')
    report=a.overview(db,id)['projects'][0];state=report['closure']
    if body.source_hash!=report['source_hash'] or state['state'] not in ('awaiting_director','approved') or state.get('accounting_id')!=body.accounting_closure_id:
        a.fail('Бухгалтер должен лично проверить актуальную версию результата',409)
    accounting=db.get(Closure,body.accounting_closure_id)
    basis=closure_basis(db,db.get(CostingEstimate,id),report,ClosureInput.model_validate(accounting.content['request']),True)
    if a.canonical(basis)!=a.canonical(accounting.content['basis']): a.fail('Основания изменились; повторите проверку бухгалтера',409)
    old=db.query(Closure).filter_by(request_id=str(body.request_id)).first()
    data=body.model_dump(mode='json')
    if old:
        if old.project_id!=id or old.actor_id!=user.id or old.stage!='director' or old.content['request']!=data: a.fail('Повторный запрос отличается',409)
        return old
    row=Closure(project_id=id,stage='director',source_hash=body.source_hash,actor_id=user.id,request_id=str(body.request_id),content={'request':data,'accounting_closure_id':body.accounting_closure_id})
    db.add(row);db.flush();return row
