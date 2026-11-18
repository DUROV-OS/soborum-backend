"""Document-backed opening-stock reconciliation for imported receipts.

Only an unconsumed receipt can be corrected without replaying downstream cost.
The source snapshot remains untouched; prior corrections form an explicit chain.
"""
from decimal import Decimal
from uuid import UUID
from fastapi import APIRouter, Depends
from pydantic import Field
from sqlalchemy import text
from sqlalchemy.orm import Session
from app.accounting.evidence_schemas import Strict, Citation
from app.common.module_access import Module
from app.core.deps import require_edit, require_view
from app.costing import governance
from app.costing.technical_basis import fingerprint
from app.db.session import get_db
from app.moysklad.importer import ref, rows, rub, moment
from app.moysklad.models import SourceRecord, SourceLink
from app.purchasing.control_models import PurchaseControlRecord as Record
from app.purchasing.models import PurchaseReturn
from app.purchasing.returns import receipt, fail, tracked
from app.purchasing.service import dec, get_order
from app.users.models import User
from app.warehouse.documents import advisory
from app.warehouse.models import WarehouseMaterial, StockMovement, StockMovementReason
from app.warehouse.service import _lock_materials, material_edit_token

router = APIRouter()
SOURCE_TYPES = ('supply', 'demand', 'loss', 'move', 'enter', 'inventory', 'processing',
                'productionstagecompletion', 'productiontask', 'purchasereturn', 'salesreturn',
                'retaildemand', 'retailsalesreturn')


def lock_sources(db):
    # Imports can insert new source documents: row locks alone do not cover them.
    if db.get_bind().dialect.name == 'postgresql':
        db.execute(text('LOCK TABLE moysklad_records, moysklad_links IN SHARE MODE'))


def references(value):
    if isinstance(value, dict):
        result = {ref(value)} - {''}
        for child in value.values(): result |= references(child)
        return result
    if isinstance(value, list):
        return set().union(*(references(child) for child in value)) if value else set()
    return set()


def complete(value):
    if isinstance(value, list): return True
    if isinstance(value, dict):
        size = (value.get('meta') or {}).get('size')
        return 'rows' in value and (size is None or size == len(value['rows']))
    return False


def line_basis(line):
    return [str(dec(line['quantity']).normalize()), str(dec(line['unit_price']).normalize()), line.get('material_id')]


def review(db, base, user=None):
    from app.purchasing.receipt_editing import token
    from app.purchasing import controls
    problems, checks = [], []
    sources = db.query(SourceRecord).filter(SourceRecord.entity_type.in_(SOURCE_TYPES)).order_by(SourceRecord.id).all()
    source_fields = {s.id: [s.payload[k] for k in ('positions', 'materials', 'products') if k in s.payload] for s in sources}
    source_refs = {s.id: set().union(*(references(f) for f in source_fields[s.id])) for s in sources}
    original = next((r for r in sources if r.entity_type == 'supply' and r.source_id == base.source_id), None)
    if not base.source_id or base.operation_id: problems.append('Эта проверка предназначена для приёмки из начального переноса.')
    if not original or not original.payload.get('applicable') or not complete(original.payload.get('positions')):
        problems.append('Нет полного проведённого оригинала приёмки с позициями.')
    if not base.supplier_id: problems.append('Не сопоставлен поставщик.')
    if base.status != 'posted': problems.append('Приёмка должна быть проведена.')
    if base.order_id and get_order(db, base.order_id).archived: problems.append('Связанный заказ находится в архиве.')
    if db.query(PurchaseReturn.id).filter_by(receipt_id=base.id).first(): problems.append('Есть возвраты: требуется перерасчёт связанных документов.')
    edits = db.query(Record).filter_by(kind='receipt_edit').order_by(Record.id).all()
    own = [r for r in edits if r.content.get('receipt_id') == base.id]
    baseline_lines = own[0].content['before']['lines'] if own else base.lines
    if own and [line_basis(l) for l in own[-1].content['after']['lines']] != [line_basis(l) for l in base.lines]:
        problems.append('Текущие строки расходятся с последней сохранённой корректировкой.')
    order_basis = None
    if base.order_id:
        order = get_order(db, base.order_id)
        order_basis = {'id': order.id, 'version': order.version, 'items': order.items}
    original_lines = rows(original.payload.get('positions')) if original else []
    if len(original_lines) != len(baseline_lines): problems.append('Состав рабочей приёмки расходится с оригиналом.')
    material_ids = {l.get('material_id') for l in base.lines if tracked(db, base, l)} - {None}
    links = db.query(SourceLink).filter_by(target_type='warehouse_materials').filter(SourceLink.target_id.in_(material_ids)).all()
    snapshots = []
    for mid in sorted(material_ids):
        material = db.get(WarehouseMaterial, mid)
        local = []
        linked = [l for l in links if l.target_id == mid and l.source_key.startswith('stock/')]
        report = None; product_id = store_id = None
        if len(linked) == 1 and len(linked[0].source_key.split('/')) == 3:
            _, product_id, store_id = linked[0].source_key.split('/')
            report = db.query(SourceRecord).filter_by(entity_type='report/stock/store/' + store_id, source_id=product_id).first()
        if not material or material.is_archived: local.append('Товар отсутствует или находится в архиве.')
        if not report or 'stock' not in report.payload or 'price' not in report.payload:
            local.append('Не найден исходный отчёт с количеством и учётной стоимостью.')
        if original and ref(original.payload.get('store')) != store_id: local.append('Склад приёмки не совпадает со складом товара.')
        originals = []
        for i, line in enumerate(baseline_lines):
            if line.get('material_id') != mid: continue
            if i >= len(original_lines): continue
            raw = original_lines[i]
            price = rub(raw.get('price')) * (1 - dec(raw.get('discount')) / 100)
            if original.payload.get('vatEnabled') and not original.payload.get('vatIncluded'):
                price *= 1 + dec(raw.get('vat')) / 100
            if (ref(raw.get('assortment')) != product_id or dec(raw.get('quantity')) != dec(line['quantity'])
                    or abs(price - dec(line['unit_price'])) > Decimal('.000001')):
                local.append('Исходная строка отличается от сохранённого оригинала.')
            originals.append(line)
        if original and original.payload.get('overhead'):
            local.append('В оригинале есть дополнительные расходы: требуется отдельный расчёт распределения.')
        related = []
        for source in sources:
            if source.entity_type == 'supply' or not original or not source.payload.get('applicable', True): continue
            raw = source.payload
            try:
                if raw.get('moment') and original.payload.get('moment') and moment(raw['moment']) < moment(original.payload['moment']): continue
            except (ValueError, TypeError):
                local.append('Не удалось проверить дату исходного документа: ' + source.title)
            fields = source_fields[source.id]
            if not fields:
                # A production order has no stock effect by itself; its completions do.
                if source.entity_type != 'productiontask': local.append('В исходных документах есть операция без раскрытого состава: ' + source.title)
                continue
            if any(not complete(field) for field in fields): local.append('Не все строки исходного документа загружены: ' + source.title)
            if product_id in source_refs[source.id]:
                related.append({'id': source.id, 'type': source.entity_type, 'number': source.title, 'date': raw.get('moment')})
        if related: local.append('После приёмки есть связанные исходные операции. Нужен перерасчёт их себестоимости.')
        allowed_ops = set(); quantity_delta = Decimal(0); value_delta = Decimal(0)
        for event in edits:
            for change in event.content.get('stock_changes', []):
                if change['material_id'] == mid:
                    allowed_ops.add(change['operation_id'])
                    quantity_delta += dec(change['delta']); value_delta += dec(change['value_delta'])
        movements = db.query(StockMovement).filter_by(warehouse_material_id=mid).order_by(StockMovement.id).all()
        initial = [m for m in movements if m.operation_id is None and m.reason == StockMovementReason.MANUAL_ADJUST and (m.note or '').startswith('Сверка остатков с МойСкладом;')]
        if any(m not in initial and m.operation_id not in allowed_ops for m in movements) or len(initial) > 1:
            local.append('После переноса есть другие складские движения: нужен перерасчёт цепочки.')
        expected_qty = expected_price = None
        if report and material:
            expected_qty = dec(report.payload.get('stock')) + quantity_delta
            value = dec(report.payload.get('stock')) * rub(report.payload.get('price')) + value_delta
            expected_price = value / expected_qty if expected_qty > 0 else Decimal(0)
            required = sum((dec(l['quantity']) for l in base.lines if l.get('material_id') == mid), Decimal(0))
            if expected_qty < required or expected_qty <= 0: local.append('Остаток меньше количества приёмки: нельзя считать всю поставку неиспользованной.')
            if (dec(material.quantity_in_stock) != expected_qty or abs(dec(material.purchase_price) - expected_price) > Decimal('.00000002')):
                local.append('Текущий остаток или учётная цена не сходятся с отчётом и журналом корректировок.')
            if any(l.get('material_id') == mid and l.get('unit') != material.unit for l in base.lines): local.append('Не совпадают единицы измерения.')
        local = list(dict.fromkeys(local))
        problems += [f'{material.title if material else mid}: {p}' for p in local]
        checks.append({'material_id': mid, 'title': material.title if material else str(mid), 'unit': material.unit if material else '',
                       'stock': str(material.quantity_in_stock) if material else None, 'price': str(material.purchase_price) if material else None,
                       'expected_stock': str(expected_qty) if expected_qty is not None else None,
                       'expected_price': str(expected_price) if expected_price is not None else None,
                       'source_report_id': report.id if report else None, 'issues': local, 'related': related})
        snapshots.append({'material_id': mid, 'token': material_edit_token(material) if material else None,
                          'report': report.payload if report else None, 'links': [l.source_key for l in linked],
                          'movements': [[m.id,str(m.delta),m.operation_id,str(m.balance_after)] for m in movements]})
    if any(tracked(db, base, l) and not l.get('material_id') for l in base.lines): problems.append('Не все складские строки сопоставлены с товарами.')
    basis = {'receipt': token(base), 'order': order_basis, 'stock': snapshots,
             'source': [[s.id, fingerprint(s.payload)] for s in sources],
             'edits': [[e.id, fingerprint(e.content)] for e in edits], 'issues': problems}
    digest = fingerprint(basis)
    accountant = governance.responsible(db, 'accounting')
    director = governance.responsible(db, 'director')
    previous = next((r for r in db.query(Record).filter_by(kind='receipt_reconcile', supplier_id=base.supplier_id).order_by(Record.id.desc()) if r.content.get('receipt_id') == base.id), None)
    approved = bool(previous and not problems and accountant and previous.actor_id == accountant.id
                    and previous.content['fingerprint'] == digest and controls.intact(db, previous, 'accounting'))
    return {'receipt_id': base.id, 'fingerprint': digest, 'issues': list(dict.fromkeys(problems)), 'materials': checks,
            'approved': approved, 'approval_id': previous.id if approved else None, 'previous_id': previous.id if previous else None,
            'accountant': {'id': accountant.id, 'name': accountant.full_name} if accountant else None,
            'director': {'id': director.id, 'name': director.full_name} if director else None,
            'can_confirm': bool(user and accountant and user.id == accountant.id),
            'can_correct': bool(user and director and user.id == director.id and approved)}


class Confirmation(Strict):
    request_id: UUID
    fingerprint: str = Field(pattern=r'^[a-f0-9]{64}$')
    evidence: Citation
    reason: str = Field(min_length=3, max_length=1000)
    confirmed: bool


@router.get('/receipts/{id}/reconciliation')
def get_review(id: int, db: Session = Depends(get_db), user: User = Depends(require_view(Module.ACCOUNTING))):
    return review(db, receipt(db, id), user)


@router.post('/receipts/{id}/reconciliation')
def confirm(id: int, body: Confirmation, db: Session = Depends(get_db), user: User = Depends(require_edit(Module.ACCOUNTING))):
    from app.purchasing import controls
    governance.require_person(db, user, 'accounting')
    if not body.confirmed: fail('Подтвердите сверку неиспользованного количества и его стоимости по документу', 422)
    advisory(db, 'receipt-reconciliation:' + str(body.request_id)); lock_sources(db)
    initial = receipt(db, id)
    if initial.order_id: get_order(db, initial.order_id, lock=True)
    base = receipt(db, id, lock=True)
    old = controls.replay(db, 'receipt_reconcile', base.supplier_id, base.order_id, body, user)
    if old:
        if old.content.get('receipt_id') != id: fail('Запрос относится к другой приёмке')
        return review(db, base, user)
    _lock_materials(db, [l['material_id'] for l in base.lines if l.get('material_id')])
    state = review(db, base, user)
    if state['fingerprint'] != body.fingerprint: fail('Основания сверки изменились. Обновите проверку.')
    if state['issues']: fail('Сначала устраните расхождения, перечисленные в проверке.')
    proofs = controls.proofs(db, base.supplier_id, [body.evidence])
    controls.record(db, 'receipt_reconcile', base.supplier_id, base.order_id, body, user,
                    receipt_id=id, fingerprint=state['fingerprint'], proofs=proofs, review=state)
    db.commit()
    return review(db, base, user)


def assert_ready(db, base, approval_id, user):
    governance.require_person(db, user, 'director')
    state = review(db, base, user)
    if not state['approved'] or state['approval_id'] != approval_id:
        fail('Татьяна должна подтвердить актуальную сверку этой приёмки. Откройте «Сверка исходного остатка».')
