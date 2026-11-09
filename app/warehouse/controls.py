"""Transactional warehouse control. A signature covers quantities, not just a label."""
from decimal import Decimal as D
from app.common.files import FileAsset, file_integrity
from app.costing.governance import require_person, responsible
from app.costing.service import fail, lock
from app.costing.technical_basis import fingerprint
from app.production.locks import lock_house_materials
from app.production.models import MaterialRequest
from app.production import readiness
from app.warehouse import service as s
from app.warehouse.control_models import WarehouseControlRecord as Record, WarehouseControlDocument as Document
from app.warehouse.models import StockMovement, StockMovementReason, WarehouseOperation, WarehouseOperationKind, IssueDestinationKind


def number(value):
    return str(D(str(value)).quantize(D('.000001')).normalize())


def document(db, file_id, locator):
    doc=db.query(Document).filter_by(file_id=file_id).first()
    asset=db.get(FileAsset,file_id) if doc else None
    if not asset or file_integrity(asset)!='matches': fail('Загрузите документ склада: файл должен существовать и совпадать с контрольной суммой',409)
    return {'file_id':file_id,'filename':asset.filename,'sha256':asset.sha256,'size_bytes':asset.size_bytes,'locator':locator}


def proof_intact(db, proof):
    asset=db.get(FileAsset,proof['file_id'])
    return bool(asset and asset.sha256==proof['sha256'] and asset.size_bytes==proof['size_bytes'] and file_integrity(asset)=='matches')


def replay(db, kind, production_id, body, actor):
    lock(db)
    row=db.query(Record).filter_by(request_id=str(body.request_id)).first()
    if row and (row.kind!=kind or row.production_id!=production_id or row.actor_id!=actor.id or row.content['request']!=body.model_dump(mode='json')):
        fail('Этот ключ операции уже использован с другими данными. Обновите форму.',409)
    return row


def record(db, kind, production_id, body, actor, **content):
    row=Record(kind=kind,production_id=production_id,request_id=str(body.request_id),actor_id=actor.id,
               content={'request':body.model_dump(mode='json'),**content})
    db.add(row);db.flush();return row


def norm(db, production_id):
    production=s.get_production_or_404(db,production_id)
    rows=s._house_block_materials(db,production_id)
    return {'production_id':production_id,'cycle_id':production.cycle_id,'lines':[
        {'block_material_id':b.id,'block_id':b.block_id,'material_id':b.warehouse_material_id,
         'title':b.warehouse_material.title,'code':b.warehouse_material.code,'unit':b.unit,
         'stock_unit':b.warehouse_material.unit,'warehouse':b.warehouse_material.warehouse.value,
         'fractional':b.warehouse_material.is_fractional,'archived':b.warehouse_material.is_archived,
         'quantity':number(D(str(b.quantity_required))+D(str(b.quantity_requested))+D(str(b.quantity_provided)))} for b in rows]}


def state(db, production_id, actor=None):
    source=norm(db,production_id);digest=fingerprint(source)
    person=responsible(db,'production')
    # Only the latest handoff counts: restoring old quantities cannot silently revive a superseded approval.
    row=db.query(Record).filter_by(kind='handoff',production_id=production_id).order_by(Record.id.desc()).first()
    ready=bool(row and person and row.actor_id==person.id and row.content['source_hash']==digest and proof_intact(db,row.content['proof']))
    return {'source_hash':digest,'norm':source,'approved':ready,'handoff_id':row.id if row else None,
        'document':row.content['proof'] if row else None,'signed_at':row.created_at if row else None,
        'responsible':{'id':person.id,'name':person.full_name} if person else None,
        'can_sign':bool(actor and person and actor.id==person.id),
        'reason':None if ready else 'Назначьте начальника производства' if not person else 'Начальник производства должен подтвердить норматив и документ' if not row else 'Норматив, ответственный или документ изменились: нужно повторное согласование'}


def handoff(db, production_id, body, actor):
    old=replay(db,'handoff',production_id,body,actor)
    if old:return old
    require_person(db,actor,'production')
    lock_house_materials(db,production_id)
    source=norm(db,production_id)
    if fingerprint(source)!=body.source_hash: fail('Норматив изменился. Откройте актуальные данные и проверьте их.',409)
    if not source['lines'] or any(D(l['quantity'])<=0 or l['archived'] or l['unit']!=l['stock_unit'] or (not l['fractional'] and D(l['quantity'])!=D(l['quantity']).to_integral_value()) for l in source['lines']):
        fail('Заполните положительные нормативы и проверьте единицы измерения и действующие материалы',409)
    if s.techcard_preview(db,s.get_production_or_404(db,production_id))['unmatched_materials_count']:
        fail('Сначала сопоставьте все материалы техкарты со складом',409)
    return record(db,'handoff',production_id,body,actor,source_hash=body.source_hash,norm=source,
                  proof=document(db,body.file_id,body.locator))


def assert_handoff(db, production_id, expected):
    current=state(db,production_id)
    if not current['approved']:fail(current['reason'],409)
    if current['source_hash']!=expected:fail('Норматив изменился. Обновите форму выдачи.',409)
    return current['handoff_id']


def issue(db, production_id, body, actor):
    old=replay(db,'issue',production_id,body,actor)
    if old:return db.get(WarehouseOperation,old.content['operation_id'])
    blocks=lock_house_materials(db,production_id)
    signed=assert_handoff(db,production_id,body.source_hash)
    before={b.id:D(str(b.quantity_provided)) for b in blocks}
    op=s.issue_by_techcard(db,s.get_production_or_404(db,production_id),actor,received_by=body.received_by,
        lines=None if body.lines is None else [(l.warehouse_material_id,l.quantity) for l in body.lines],note=body.note,occurred_at=body.occurred_at)
    movements=db.query(StockMovement).filter_by(operation_id=op.id).all()
    allocations=[{'movement_id':m.id,'blocks':[{'block_material_id':b.id,'quantity':number(D(str(b.quantity_provided))-before[b.id])}
        for b in blocks if b.warehouse_material_id==m.warehouse_material_id and D(str(b.quantity_provided))>before[b.id]]} for m in movements]
    record(db,'issue',production_id,body,actor,operation_id=op.id,handoff_id=signed,allocations=allocations)
    return op


def issue_request(db, request_id, body, actor):
    lock(db)
    req=db.query(MaterialRequest).filter_by(id=request_id).with_for_update().populate_existing().first()
    if not req:fail('Заявка не найдена',404)
    production_id=req.block_material.block.production_id
    old=replay(db,'request_issue',production_id,body,actor)
    if old:
        if old.content['request_id']!=request_id:fail('Ключ относится к другой заявке',409)
        return req
    lock_house_materials(db,production_id)
    signed=assert_handoff(db,production_id,body.source_hash)
    s.approve_request(db,req,actor,quantity=body.quantity)
    movement=db.get(StockMovement,req._issued_movement_id)
    record(db,'request_issue',production_id,body,actor,request_id=request_id,handoff_id=signed,received_by=body.received_by,
           allocations=[{'movement_id':movement.id,'blocks':[{'block_material_id':req.block_material_id,'quantity':number(-D(str(movement.delta)))}]}])
    return req


def returns_for(db, movement_id):
    return [r for r in db.query(Record).filter_by(kind='return').order_by(Record.id) if r.content['movement_id']==movement_id]


def returned_quantity(db, movement_id):
    return sum((D(r.content['request']['quantity']) for r in returns_for(db,movement_id)),D(0))


def return_facts(db, movement_id, records=None):
    return [{'id':r.id,'quantity':r.content['request']['quantity'],'operation_id':r.content['operation_id'],
             'proof':r.content['proof'],'intact':proof_intact(db,r.content['proof'])} for r in (records if records is not None else returns_for(db,movement_id))]


def original_allocations(db, movement, records=None):
    rows=records if records is not None else db.query(Record).filter(Record.kind.in_(['issue','request_issue'])).order_by(Record.id)
    for row in rows:
        for allocation in row.content['allocations']:
            if allocation['movement_id']==movement.id:return row.production_id,allocation['blocks']
    # Older request issues identify exactly one block; old aggregated techcard issues do not.
    if movement.reason==StockMovementReason.ISSUED:
        req=db.get(MaterialRequest,movement.reference_id)
        if req:return req.block_material.block.production_id,[{'block_material_id':req.block_material_id,'quantity':number(-D(str(movement.delta)))}]
    fail('У прежней выдачи нет точного распределения по блокам. Требуется сверка с начальником производства; автоматический возврат недоступен.',409)


def return_material(db, movement_id, body, actor):
    lock(db)
    movement=db.get(StockMovement,movement_id)
    if not movement or movement.reason not in (StockMovementReason.ISSUED,StockMovementReason.ISSUED_TECHCARD) or D(str(movement.delta))>=0:
        fail('Исходная выдача в производство не найдена',404)
    production_id,allocations=original_allocations(db,movement)
    old=replay(db,'return',production_id,body,actor)
    if old:
        if old.content['movement_id']!=movement_id:fail('Ключ относится к другой выдаче',409)
        return db.get(WarehouseOperation,old.content['operation_id'])
    blocks={b.id:b for b in lock_house_materials(db,production_id)}
    previous=returns_for(db,movement_id)
    if body.quantity+sum((D(r.content['request']['quantity']) for r in previous),D(0))>-D(str(movement.delta)):
        fail('Возврат превышает количество исходной выдачи за вычетом прежних возвратов',409)
    proof=document(db,body.file_id,body.locator)
    when=s.resolve_occurred_at(body.occurred_at)
    issued_at=movement.operation.occurred_at if movement.operation else movement.created_at
    from datetime import timezone
    if when.astimezone(timezone.utc) < (issued_at.replace(tzinfo=timezone.utc) if issued_at.tzinfo is None else issued_at.astimezone(timezone.utc)):fail('Возврат не может быть раньше выдачи')
    left=body.quantity;restored=[]
    for allocation in allocations:
        bid=allocation['block_material_id'];b=blocks.get(bid)
        used=sum((D(item['quantity']) for r in previous for item in r.content['restored'] if item['block_material_id']==bid),D(0))
        take=min(left,D(allocation['quantity'])-used)
        if take<=0:continue
        if not b or b.warehouse_material_id!=movement.warehouse_material_id or b.unit!=movement.warehouse_material.unit or D(str(b.quantity_provided))<take:
            fail('Выдача больше не совпадает с блоками производства. Требуется сверка.',409)
        b.quantity_provided=float(D(str(b.quantity_provided))-take)
        b.quantity_required=float(D(str(b.quantity_required))+take)
        restored.append({'block_material_id':bid,'quantity':number(take)});left-=take
    if left:fail('Распределение исходной выдачи неполное. Требуется сверка.',409)
    op=s.create_stock_operation(db,WarehouseOperationKind.RECEIPT,[(movement.warehouse_material_id,float(body.quantity))],actor,
        occurred_at=when,note=f'Возврат выдачи №{movement.id}: {body.reason}'[:500],destination_kind=IssueDestinationKind.HOUSE,
        production_id=production_id,destination=s.house_label(s.get_production_or_404(db,production_id)),received_by=body.received_by)
    record(db,'return',production_id,body,actor,movement_id=movement_id,operation_id=op.id,proof=proof,restored=restored)
    readiness.invalidate_production_caches(db,production_id)
    return op
