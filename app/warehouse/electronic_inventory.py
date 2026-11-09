"""Personally recorded counts, immutable observations and a generated electronic act."""
from datetime import datetime, timezone
from decimal import Decimal
from html import escape
from uuid import UUID
from typing import Literal
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session
from app.common.module_access import Module
from app.common.files import save_bytes_file, FilePurpose
from app.core.deps import require_edit, require_view
from app.db.session import get_db
from app.warehouse import documents as d, controls as c, inventory_evidence as e
from app.warehouse.control_models import WarehouseInventoryCount as Count, WarehouseInventoryAct as Act, WarehouseControlDocument
from app.costing.technical_basis import fingerprint

router = APIRouter(prefix='/inventory-counts', tags=['Электронная инвентаризация'])


class Observation(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    request_id: UUID
    version: int = Field(ge=1)
    material_id: int = Field(gt=0)
    quantity: Decimal = Field(ge=0, le=d.MAX_QTY, decimal_places=6, allow_inf_nan=False)
    comment: str = Field(default='', max_length=1000)
    file_id: int | None = Field(default=None, gt=0)


class Confirm(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: UUID
    version: int = Field(ge=1)
    confirmed: Literal[True]


def utc(value):
    return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)).isoformat()


def basis(line):
    return {k: line[k] for k in ('material_id','unit','is_fractional','expected_quantity','expected_movement_id','quantity')}


def count_out(row):
    return {'id':row.id, 'document_id':row.document_id, 'material_id':row.material_id,
            'actor_id':row.actor_id, 'created_at':utc(row.created_at), 'content':row.content}


def save_count(db, row, body, user):
    prior=db.query(Count).filter_by(request_id=str(body.request_id)).first()
    if prior:
        if prior.document_id!=row.id or prior.actor_id!=user.id or prior.content['request']!=body.model_dump(mode='json'):
            d.fail('Этот ключ пересчёта уже использован с другими данными')
        return prior
    d.draft(row,body.version)
    if row.kind!='inventory': d.fail('Пересчёт доступен только для инвентаризации',422)
    line=next((line for line in row.lines if line['material_id']==body.material_id),None)
    if not line: d.fail('Позиция не входит в эту инвентаризацию',422)
    material=c.s._lock_materials(db,[body.material_id]).get(body.material_id)
    if not material or material.is_archived or material.warehouse.value!=row.warehouse or material.unit!=line['unit'] or material.is_fractional!=line['is_fractional']:
        d.fail('Карточка материала изменилась. Проверьте состав инвентаризации.')
    if not material.is_fractional and body.quantity!=body.quantity.to_integral_value(): d.fail('Введите целое количество',422)
    # A fresh physical observation takes its own baseline. Later movements still block posting.
    updated={**line,'expected_quantity':str(d.dec(material.quantity_in_stock)),
        'expected_movement_id':d.movement_watermarks(db,[material.id]).get(material.id,0), 'quantity':str(body.quantity)}
    proof=c.document(db,body.file_id,'Приложение к фактическому пересчёту позиции') if body.file_id else None
    count=Count(document_id=row.id,material_id=material.id,request_id=str(body.request_id),actor_id=user.id,
        content={'request':body.model_dump(mode='json'),'basis':basis(updated),'actor_name':user.full_name,
                 'comment':body.comment,'proof':proof,'previous_count_id':line.get('count_id')})
    db.add(count);db.flush()
    updated.update(count_id=count.id,counted_by=user.full_name,counted_at=utc(count.created_at),count_comment=body.comment,count_proof=proof)
    row.lines=[updated if item['material_id']==material.id else item for item in row.lines]
    row.version+=1;db.flush()
    return count


def observations(db,row):
    ids=[line.get('count_id') for line in row.lines]
    records={r.id:r for r in db.query(Count).filter(Count.id.in_([i for i in ids if i])).all()}
    result=[]
    for line in row.lines:
        count=records.get(line.get('count_id'))
        if not count or count.document_id!=row.id or count.material_id!=line['material_id'] or count.content['basis']!=basis(line):
            d.fail('Пересчитайте каждую позицию в электронном режиме и сохраните результат',422)
        if count.content['proof'] and not c.proof_intact(db,count.content['proof']): d.fail('Приложение к пересчёту повреждено. Повторно подтвердите позицию.')
        result.append(count_out(count))
    return result


def observations_intact(db, content):
    if content.get('mode')!='electronic': return True
    rows=content.get('observations',[])
    if not rows: return False
    found={r.id:r for r in db.query(Count).filter(Count.id.in_([r['id'] for r in rows])).all()}
    return all(r['id'] in found and count_out(found[r['id']])==r and
        (not r['content']['proof'] or c.proof_intact(db,r['content']['proof'])) for r in rows)


def render_act(source, counts, user, confirmed_at):
    h=lambda value:escape(str(value),quote=True)
    records={r['id']:r for r in counts}
    lines=[]
    for line in source['lines']:
        count=records[line['count_id']]
        delta=d.dec(line['quantity'])-d.dec(line['expected_quantity'])
        lines.append('<tr>'+''.join('<td>'+h(v)+'</td>' for v in [line['title'],line['code'],line['unit'],line['expected_quantity'],line['quantity'],str(delta),count['content']['actor_name'],count['created_at'],count['content']['comment'],count['content']['proof']['filename'] if count['content']['proof'] else ''])+'</tr>')
    return ('<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Электронный акт '+h(source['number'])+'</title><style>body{font:15px system-ui;margin:24px;color:#20372d}table{border-collapse:collapse;width:100%}th,td{border:1px solid #bbc9c1;padding:8px;text-align:left}th{background:#eef3ef}p{line-height:1.5}.table{overflow:auto}@media print{body{font:10px sans-serif;margin:0}thead{display:table-header-group}tr{break-inside:avoid}}</style>'
        '<h1>Электронный акт инвентаризации '+h(source['number'])+'</h1><p>Склад: '+h(source['warehouse'])+'<br>Основание: '+h(source['note'])+'</p>'
        '<p>Результаты подтвердил: '+h(user.full_name)+'<br>Время подтверждения (UTC): '+h(confirmed_at)+'<br>Версия документа: '+h(source['version'])+'</p>'
        '<div class="table"><table><thead><tr>'+''.join('<th>'+h(v)+'</th>' for v in ['Материал','Код','Ед.','Учёт','Факт','Разница','Пересчитал','Время (UTC)','Комментарий','Приложение'])+'</tr></thead><tbody>'+''.join(lines)+'</tbody></table></div>'
        '<p>Результаты физического пересчёта подтверждены в учётной записи сотрудника. Проведение и изменение остатков фиксируются отдельно в системе. Для печати используйте меню браузера.</p></html>')


def prepare(db,row,body,user):
    prior=db.query(Act).filter_by(request_id=str(body.request_id)).first()
    request=body.model_dump(mode='json')
    if prior:
        if prior.document_id!=row.id or prior.actor_id!=user.id or prior.content.get('mode')!='electronic' or prior.content['request']!=request:
            d.fail('Этот ключ подтверждения уже использован с другими данными')
        return prior
    d.draft(row,body.version)
    if row.kind!='inventory' or not row.lines or not row.note.strip(): d.fail('Выберите инвентаризацию с основанием и позициями',422)
    counts=observations(db,row)
    materials=c.s._lock_materials(db,[line['material_id'] for line in row.lines])
    watermarks=d.movement_watermarks(db,list(materials))
    for line in row.lines:
        material=materials.get(line['material_id'])
        if not material or material.unit!=line['unit'] or material.is_fractional!=line['is_fractional'] or material.warehouse.value!=row.warehouse or material.is_archived or d.dec(material.quantity_in_stock)!=d.dec(line['expected_quantity']) or watermarks.get(material.id,0)!=line['expected_movement_id']:
            d.fail('После пересчёта были изменения: повторно пересчитайте «'+line['title']+'».')
    source=e.snapshot(row);confirmed_at=datetime.now(timezone.utc).isoformat()
    file=save_bytes_file(db,'Электронный акт '+row.number+'.html','text/html; charset=utf-8',render_act(source,counts,user,confirmed_at).encode('utf-8'),FilePurpose.COSTING_EVIDENCE,user)
    db.add(WarehouseControlDocument(file_id=file.id,actor_id=user.id));db.flush()
    act=Act(document_id=row.id,stage='prepared',request_id=str(body.request_id),actor_id=user.id,file_id=file.id,
        content={'mode':'electronic','request':request,'snapshot':source,'source_hash':fingerprint(source),
                 'proof':c.document(db,file.id,'Электронная ведомость, все сохранённые позиции'),'observations':counts,'confirmed_at':confirmed_at})
    db.add(act);db.flush();return act


@router.get('/{document_id}')
def history(document_id:int,db:Session=Depends(get_db),user=Depends(require_view(Module.WAREHOUSE))):
    d.get(db,document_id)
    return [count_out(row) for row in db.query(Count).filter_by(document_id=document_id).order_by(Count.id.desc())]


@router.post('/{document_id}')
def record(document_id:int,body:Observation,db:Session=Depends(get_db),user=Depends(require_edit(Module.WAREHOUSE))):
    d.advisory(db,'inventory-count:'+str(body.request_id));row=d.get(db,document_id,True)
    count=save_count(db,row,body,user);db.commit()
    return {'document':d.out(row),'observation':count_out(count)}


@router.post('/{document_id}/confirm',status_code=201)
def confirm(document_id:int,body:Confirm,db:Session=Depends(get_db),user=Depends(require_edit(Module.WAREHOUSE))):
    d.advisory(db,'inventory-evidence:'+str(body.request_id));row=d.get(db,document_id,True)
    act=prepare(db,row,body,user);db.commit();return e.act_out(db,act,row)
