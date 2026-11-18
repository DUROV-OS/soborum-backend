from datetime import datetime, timezone
from decimal import Decimal
from io import BytesIO, StringIO
from zoneinfo import ZoneInfo
import csv
from fastapi import HTTPException
from sqlalchemy import or_
from app.accounting.models import SupplierOrder, MoneyMovement, MoneyDirection
from app.purchasing.models import PurchaseReceipt
from app.moysklad.models import SourceLink, SourceRecord
from app.moysklad.service import allowed as source_allowed
from app.common.module_access import AccessLevel, Module
from .sync import source_id

TYPES={'purchaseorder':'Заказ поставщику','supply':'Приёмка','facturein':'Счёт-фактура','paymentout':'Исходящий платёж'}


def ref(value):
    if not isinstance(value,dict):return None
    return value.get('id') or value.get('meta',{}).get('href','').split('?')[0].rstrip('/').rsplit('/',1)[-1]


def documents(db,user,cp,types='',date_from=None,date_to=None):
    if date_from and date_to and date_from>date_to:raise HTTPException(422,'Начало периода позже окончания')
    selected=set(types.split(',')) if types else set(TYPES)
    if not selected<=set(TYPES):raise HTTPException(422,'Неизвестный тип документа')
    finance=user.access_level(Module.ACCOUNTING)>=AccessLevel.VIEW
    rows=[];represented=set();warnings=[]
    def append(type,id,number,when,amount,currency,status,url,source=None):
        if source:represented.add((type,source))
        if type not in selected:return
        if isinstance(when,str):
            try:when=datetime.fromisoformat(when)
            except ValueError:when=None
        if isinstance(when,datetime):
            when=when.replace(tzinfo=ZoneInfo('Europe/Moscow')) if when.tzinfo is None else when.astimezone(ZoneInfo('Europe/Moscow'))
            day=when.date()
        else:day=when
        if (date_from and (not day or day<date_from)) or (date_to and (not day or day>date_to)):return
        rows.append({'key':type+':'+str(id),'type':type,'type_label':TYPES[type],'number':number or str(id),'date':day.isoformat() if day else '',
                     'amount':str(Decimal(str(amount)).quantize(Decimal('.01'))) if amount is not None else None,
                     'currency':currency or '', 'status':str(status),'url':url,'source':'МойСклад' if str(id).startswith('source-') else 'Durov OS'})
    if finance:
        orders=db.query(SupplierOrder).filter_by(supplier_id=cp.supplier_id).all() if cp.supplier_id else []
        for r in orders:append('purchaseorder',r.id,r.number,r.created_at,r.total_cost,r.currency,r.source_state or r.status.value,f'/purchasing?tab=orders&order={r.id}',r.source_id)
        receipts=db.query(PurchaseReceipt).filter_by(supplier_id=cp.supplier_id).all() if cp.supplier_id else []
        for r in receipts:
            amount=sum((Decimal(str(l['quantity']))*Decimal(str(l['unit_price'])) for l in r.lines),Decimal(0)) if all('quantity' in l and 'unit_price' in l for l in r.lines) else None
            currency=next((o.currency for o in orders if o.id==r.order_id),r.parameters.get('currency',''))
            append('supply',r.id,r.number,r.occurred_at,amount,currency,r.status,f'/purchasing?tab=receipts&receipt={r.id}',r.source_id)
        payments=db.query(MoneyMovement).filter(MoneyMovement.direction==MoneyDirection.EXPENSE,or_(MoneyMovement.counterparty_id==cp.id,MoneyMovement.supply_id.in_([o.id for o in orders]))).all()
        links={l.target_id:l.source_key.split('/',1)[1] for l in db.query(SourceLink).filter_by(target_type='money_movements') if l.source_key.startswith('paymentout/')}
        for r in payments:append('paymentout',r.id,r.external_number,r.doc_date or r.posted_at or r.created_at,r.amount,r.currency,r.status.value,f'/accounting?movement={r.id}',links.get(r.id))
    else:warnings.append('Для закупочных документов и платежей нужен доступ к бухгалтерии. Импортированные приёмки доступны по правам склада.')
    try: sid=source_id(db,cp)
    except HTTPException:sid=None
    if sid:
        matched=[]
        for r in db.query(SourceRecord).filter(SourceRecord.entity_type.in_(selected)):
            if not source_allowed(user,r.entity_type) or ref(r.payload.get('agent'))!=sid:continue
            matched.append(r.entity_type)
            if (r.entity_type,r.source_id) in represented:continue
            raw=r.payload
            append(r.entity_type,'source-'+str(r.id),raw.get('name') or r.title,raw.get('moment'),Decimal(str(raw['sum']))/100 if raw.get('sum') is not None else None,
                   raw.get('_currency_code') or '',r.state or ('Проведён' if raw.get('applicable') else 'Не проведён'),f'/moysklad?type={r.entity_type}&record={r.id}',r.source_id)
        if 'facturein' in selected and 'facturein' not in matched:warnings.append('В сохранённых данных нет связанных счетов-фактур. Это не подтверждает их отсутствие в МойСклад.')
    else:warnings.append('Нет однозначной связи с МойСклад: показаны только локальные документы.')
    rows.sort(key=lambda r:(r['date'],r['key']),reverse=True)
    return rows,warnings


def export(rows,format):
    columns=['Тип','Номер','Дата','Сумма','Валюта','Статус','Источник','Ссылка']
    def safe(value):
        value=str(value or '')
        return "'"+value if value.lstrip().startswith(('=','+','-','@','\t','\r')) else value
    values=[[r['type_label'],r['number'],r['date'],r['amount'] or '',r['currency'],r['status'],r['source'],r['url']] for r in rows]
    if format=='csv':
        stream=StringIO();writer=csv.writer(stream,delimiter=';');writer.writerow(columns)
        writer.writerows([[safe(v) for v in row] for row in values])
        return ('\ufeff'+stream.getvalue()).encode(),'text/csv; charset=utf-8'
    from openpyxl import Workbook
    book=Workbook();sheet=book.active;sheet.title='Документы';sheet.append(columns)
    for row in values:
        sheet.append([safe(v) for v in row])
        if row[3]:sheet.cell(sheet.max_row,4,Decimal(row[3])).number_format='#,##0.00'
    sheet.freeze_panes='A2';sheet.auto_filter.ref=sheet.dimensions
    stream=BytesIO();book.save(stream)
    return stream.getvalue(),'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
