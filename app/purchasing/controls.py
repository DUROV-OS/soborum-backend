"""Document-backed supplier controls. No bank or supplier messaging side effects."""
from datetime import datetime, timezone, timedelta
from decimal import Decimal as D
from fastapi import HTTPException
from app.common.files import FileAsset, FilePurpose, file_integrity
from app.costing import governance
from app.costing.models import CostingEstimate, CostingRevision
from app.costing.technical_basis import fingerprint as digest
from app.purchasing.control_models import PurchaseControlRecord as Record, PurchaseControlDocument as Document
from app.purchasing.models import PurchaseReceipt
from app.purchasing.returns import returned_lines
from app.accounting.models import SupplierOrder
from app.sales.models import SalesOrder
from app.sales import journey_supply as supply
from app.warehouse.models import Supplier, WarehouseMaterial


def today():
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo('Europe/Moscow')).date()


def fail(message, code=409):
    raise HTTPException(code, message)


def lock(db):
    from app.warehouse.documents import advisory
    from app.costing.service import lock as cost_lock
    advisory(db, 'sales-supply-allocations')
    cost_lock(db)
    advisory(db, 'purchasing-controls')


def supplier(db, id):
    row = db.get(Supplier, id)
    if not row:
        fail('Поставщик не найден', 404)
    return row


def supplier_basis(row):
    return digest({'id': row.id, 'name': row.name, 'contacts': row.contacts, 'status': row.status.value})


def order_basis(row):
    return digest({'version': row.version, 'supplier_id': row.supplier_id, 'currency': row.currency,
                   'lines': [supply.supply_basis(row, i) for i in range(len(row.items))]})


def latest(db, kind, supplier_id, order_id=None):
    return db.query(Record).filter_by(kind=kind, supplier_id=supplier_id, order_id=order_id).order_by(Record.id.desc()).first()


def replay(db, kind, sid, oid, body, user):
    old = db.query(Record).filter_by(request_id=str(body.request_id)).first()
    if old and (old.kind != kind or old.supplier_id != sid or old.order_id != oid or old.actor_id != user.id
                or old.content['request'] != body.model_dump(mode='json')):
        fail('Номер запроса уже использован для другого действия')
    return old


def record(db, kind, sid, oid, body, user, **data):
    row = Record(kind=kind, supplier_id=sid, order_id=oid, actor_id=user.id,
                 request_id=str(body.request_id), content={'request': body.model_dump(mode='json'), **data})
    db.add(row); db.flush()
    return row


def documents(db, sid):
    return {a.id: a for a in db.query(FileAsset).join(Document, Document.file_id == FileAsset.id)
            .filter(Document.supplier_id == sid, FileAsset.purpose == FilePurpose.COSTING_EVIDENCE)}


def proofs(db, sid, citations):
    available = documents(db, sid)
    result = {}
    for citation in citations:
        asset = available.get(citation.file_id)
        if not asset or file_integrity(asset) != 'matches':
            fail('Загрузите целый документ именно этого поставщика', 422)
        result[str(asset.id)] = {'sha256': asset.sha256, 'size_bytes': asset.size_bytes}
    return result


def intact(db, row, stage):
    person = governance.responsible(db, stage)
    if not row or not person or row.actor_id != person.id:
        return False
    available = documents(db, row.supplier_id)
    for fid, expected in row.content.get('proofs', {}).items():
        asset = available.get(int(fid))
        if not asset or asset.sha256 != expected['sha256'] or asset.size_bytes != expected['size_bytes'] or file_integrity(asset) != 'matches':
            return False
    return bool(row.content.get('proofs'))


def trust_state(db, row):
    event = latest(db, 'trust', row.id)
    ready = bool(event and event.content['request']['trusted'] and intact(db, event, 'director')
                 and event.content['supplier_basis'] == supplier_basis(row) and row.status.value == 'active')
    return event, ready


def trust(db, sid, body, user):
    governance.require_person(db, user, 'director')
    lock(db)
    old = replay(db, 'trust', sid, None, body, user)
    if old:
        return old
    row = supplier(db, sid)
    last = latest(db, 'trust', sid)
    if (last.id if last else 0) != body.expected_id:
        fail('Статус поставщика изменился. Обновите карточку.')
    if body.trusted and row.status.value != 'active':
        fail('Архивного поставщика нельзя подтвердить')
    return record(db, 'trust', sid, None, body, user, supplier_basis=supplier_basis(row),
                  proofs=proofs(db, sid, [body.evidence]))


def price_state(db, row):
    event = latest(db, 'price', row.supplier_id, row.id)
    valid = bool(event and intact(db, event, 'accounting') and event.content['basis'] == order_basis(row))
    return event, valid


def receipt_basis(row):
    return digest({'id': row.id, 'supplier_id': row.supplier_id, 'order_id': row.order_id, 'status': row.status,
                   'occurred_at': (row.occurred_at.replace(tzinfo=timezone.utc) if row.occurred_at.tzinfo is None else row.occurred_at.astimezone(timezone.utc)).isoformat(), 'number': row.number, 'lines': row.lines, 'operation_id': row.operation_id})


def receipt_price_matches(line, item):
    try:
        actual = D(str(line.get('unit_price')))
        ordered = D(str(item.get('unit_price')))
        return actual.is_finite() and actual > 0 and actual == ordered
    except (ValueError, ArithmeticError):
        return False


def price(db, row, body, user):
    governance.require_person(db, user, 'accounting')
    old = replay(db, 'price', row.supplier_id, row.id, body, user)
    if old:
        return old
    last = latest(db, 'price', row.supplier_id, row.id)
    if row.version != body.version or (last.id if last else 0) != body.expected_id:
        fail('Заказ или проверка изменились. Обновите карточку.')
    if body.document_date > today():
        fail('Документ не может быть датирован будущим', 422)
    if {l.index for l in body.lines} != set(range(len(row.items))) or len(body.lines) != len(row.items):
        fail('Подтвердите каждую строку заказа ровно один раз', 422)
    for line in body.lines:
        item = row.items[line.index]
        material = db.get(WarehouseMaterial, item.get('warehouse_material_id'))
        if not material or material.is_archived or material.unit != item.get('unit') or not item.get('stock_tracked', True):
            fail('Сопоставьте каждую позицию с действующим материалом и единицей склада', 422)
        if not line.min_quantity <= D(str(item['quantity'])) <= line.max_quantity or D(str(item['unit_price'])) <= 0:
            fail('Цена должна быть положительной, а количество — внутри подтверждённого диапазона партии', 422)
    citations = [l.evidence for l in body.lines]
    receipt = None
    if body.receipt_id:
        receipt = db.get(PurchaseReceipt, body.receipt_id)
        if not receipt or receipt.order_id != row.id or receipt.supplier_id != row.supplier_id or receipt.status != 'posted':
            fail('Укажите проведённую приёмку этого заказа', 422)
        if receipt.occurred_at.date() > today():
            fail('Приёмка датирована будущим', 422)
        for received in receipt.lines:
            index = received.get('order_line')
            if (not isinstance(index, int) or index < 0 or index >= len(row.items)
                    or received.get('material_id') != row.items[index].get('warehouse_material_id')
                    or not receipt_price_matches(received, row.items[index])):
                fail('Фактическая цена или позиция приёмки не совпадает с заказом. Сверьте документы; цена заказа не заменяет фактическую цену.', 422)
        citations.append(body.receipt_evidence)
    return record(db, 'price', row.supplier_id, row.id, body, user, basis=order_basis(row),
                  receipt_basis=receipt_basis(receipt) if receipt else None, proofs=proofs(db, row.supplier_id, citations))


def offer(db, sid, body, user):
    governance.require_person(db, user, 'accounting')
    lock(db)
    old = replay(db, 'offer', sid, None, body, user)
    if old:
        return old
    row = supplier(db, sid)
    material = db.get(WarehouseMaterial, body.material_id)
    if row.status.value != 'active' or not material or material.is_archived or material.unit != body.unit:
        fail('Проверьте поставщика, материал и единицу измерения', 422)
    if body.document_date > today():
        fail('Предложение не может быть датировано будущим', 422)
    return record(db, 'offer', sid, None, body, user, supplier_basis=supplier_basis(row),
                  proofs=proofs(db, sid, [body.evidence]))


def comparable(a, b, quantity, previous_quantity=None):
    keys = ('vat', 'destination', 'payment_terms')
    if D(a['vat_rate']) != D(b['vat_rate']):
        return False
    if any(str(a[k]).strip().casefold() != str(b[k]).strip().casefold() for k in keys):
        return False
    amounts = [quantity] + ([] if previous_quantity is None else [previous_quantity])
    return all(D(t['min_quantity']) <= q <= D(t['max_quantity']) for t in (a, b) for q in amounts)


def historical(db, row, index):
    """Latest actual purchase first. Never skip an unverified newer purchase."""
    item = row.items[index]
    for receipt in db.query(PurchaseReceipt).filter_by(supplier_id=row.supplier_id, status='posted').order_by(
            PurchaseReceipt.occurred_at.desc(), PurchaseReceipt.id.desc()):
        if receipt.order_id == row.id or receipt.occurred_at.date() > today():
            continue
        previous = db.get(SupplierOrder, receipt.order_id) if receipt.order_id else None
        for ri, line in enumerate(receipt.lines):
            if line.get('material_id') != item.get('warehouse_material_id'):
                continue
            pi = line.get('order_line')
            if not previous or not isinstance(pi, int) or pi < 0 or pi >= len(previous.items):
                return {'receipt_id': receipt.id}, None, None
            if D(str(line['quantity'])) <= returned_lines(db, receipt.id)[ri]:
                continue
            proof, valid = price_state(db, previous)
            base = {'order_id': previous.id, 'number': previous.number or str(previous.id), 'receipt_id': receipt.id,
                    'line': pi, 'price_record_id': proof.id if proof else None}
            if not receipt_price_matches(line, previous.items[pi]):
                base['issue'] = 'Фактическая цена приёмки отличается от заказа или не указана. Нужна сверка документов.'
                return base, None, None
            if (not valid or proof.content['request']['receipt_id'] != receipt.id
                    or proof.content['receipt_basis'] != receipt_basis(receipt)):
                return base, None, None
            if previous.currency != row.currency or previous.items[pi].get('unit') != item.get('unit'):
                return base, None, None
            if previous.items[pi].get('warehouse_material_id') != item.get('warehouse_material_id'):
                return base, None, None
            terms = next(t for t in proof.content['request']['lines'] if t['index'] == pi)
            base['unit_price'] = str(previous.items[pi]['unit_price'])
            base['delivered_unit_price'] = str(D(base['unit_price']) + D(terms['delivery_per_unit']))
            return base, terms, D(str(previous.items[pi]['quantity']))
    return None, None, None


def sale_checks(db, row, index, allocations):
    from app.costing.integration import link_out
    from app.sales.journey import checked_runs
    selected = [a for a in allocations if a.data['supplier_order_id'] == row.id and a.data['supplier_line'] == index]
    problems, targets = [], []
    item = row.items[index]
    if sum((D(a.data['quantity']) for a in selected), D(0)) != D(str(item['quantity'])):
        problems.append('Закрепите всё количество за строками согласованных продаж')
    for a in selected:
        order = db.get(SalesOrder, a.order_id)
        if not order or order.archived or order.status != 'confirmed' or a.data['order_line'] >= len(order.lines):
            problems.append('Связанная продажа недоступна или не подтверждена'); continue
        demand, link, recipe = supply.needs(db, order, a.data['order_line'])
        if not link or not link_out(db, link, order)['fresh']:
            problems.append('Связь продажи с утверждённой сметой устарела'); continue
        estimate = db.get(CostingEstimate, link.estimate_id)
        revision = db.get(CostingRevision, link.revision_id)
        if (not estimate or estimate.archived or not revision or revision.estimate_id != estimate.id
                or revision.number != estimate.version):
            problems.append('Продажа закреплена за прежней версией сметы; нужна сверка с действующей версией'); continue
        try:
            governance.assert_approved(db, estimate)
        except HTTPException:
            problems.append('Нет действующего согласования Ильи, Татьяны и руководителя'); continue
        if not recipe or recipe.archived or recipe.content.get('warnings') or not supply.state(db, a, order, recipe)['fresh']:
            problems.append('Нет действующей техкарты или закрепление закупки устарело'); continue
        runs, invalid = checked_runs(db, recipe)
        consumed = sum((D(r['fraction']) for r in runs), D(0))
        target = [x for x in allocations if x.order_id == a.order_id and x.data['order_line'] == a.data['order_line']
                  and x.data['material_id'] == item.get('warehouse_material_id')]
        assigned = sum((D(x.data['quantity']) for x in target), D(0))
        if invalid or consumed > 1 or assigned > demand.get(item.get('warehouse_material_id'), D(0)) * (1 - consumed):
            problems.append('Закреплённые закупки и уже выданные по техкарте материалы превышают оставшуюся норму')
        targets.append({'allocation_id': a.id, 'order_id': order.id, 'number': order.number,
                        'quantity': a.data['quantity'], 'estimate_id': estimate.id, 'revision': link.revision_id,
                        'recipe_id': recipe.id, 'recipe_version': recipe.version, 'consumed': str(consumed),
                        'assigned': str(assigned)})
    return list(dict.fromkeys(problems)), targets


def offers(db, row, item, terms):
    result = []
    seen = set()
    for event in db.query(Record).filter_by(kind='offer').order_by(Record.id.desc()):
        data = event.content['request']
        if data['material_id'] != item.get('warehouse_material_id') or event.supplier_id in seen:
            continue
        seen.add(event.supplier_id)
        supplier_row = supplier(db, event.supplier_id)
        if (event.supplier_id == row.supplier_id or not intact(db, event, 'accounting')
                or event.content['supplier_basis'] != supplier_basis(supplier_row)
                or supplier_row.status.value != 'active' or data['currency'] != row.currency
                or data['unit'] != item.get('unit') or data['valid_until'] < today().isoformat()
                or data['document_date'] < (today() - timedelta(days=14)).isoformat()
                or not comparable(terms, data, D(str(item['quantity'])))):
            continue
        result.append({'id': event.id, 'supplier_id': event.supplier_id, 'supplier': supplier_row.name,
                       'unit_price': data['unit_price'], 'delivered_unit_price': str(D(data['unit_price']) + D(data['delivery_per_unit'])),
                       'valid_until': data['valid_until'], 'document_date': data['document_date'],
                       'evidence': data['evidence']})
    return sorted(result, key=lambda o: D(o['delivered_unit_price']))


def review(db, row, user=None):
    supplier_row = supplier(db, row.supplier_id)
    trust_record, trusted = trust_state(db, supplier_row)
    price_record, verified = price_state(db, row)
    reasons = []
    def problem(code, message):
        reasons.append({'code': code, 'message': message})
    if not trusted:
        problem('supplier_not_verified', 'Руководитель должен подтвердить поставщика документом; данные должны оставаться актуальными.')
    if row.archived:
        problem('archived', 'Заказ находится в архиве.')
    if not row.items:
        problem('no_items', 'В заказе нет позиций.')
    if not verified:
        problem('price_not_verified', 'Татьяна должна проверить цены и условия каждой строки по документам.')
    elif price_record.content['request']['valid_until'] < today().isoformat():
        problem('price_expired', 'Срок действия подтверждённой цены истёк.')
    allocations = supply.active_links(db)
    lines = []
    for i, item in enumerate(row.items):
        issues, targets = sale_checks(db, row, i, allocations)
        material = db.get(WarehouseMaterial, item.get('warehouse_material_id'))
        if not material or material.is_archived or item.get('unit') != material.unit or not item.get('stock_tracked', True):
            issues.append('Материал или единица склада не сопоставлены')
        prior, prior_terms, prior_quantity = historical(db, row, i)
        terms = next((t for t in price_record.content['request']['lines'] if t['index'] == i), None) if verified else None
        delivered = None
        market = []
        if terms:
            delivered = D(str(item['unit_price'])) + D(terms['delivery_per_unit'])
            market = offers(db, row, item, terms)
        if not prior:
            issues.append('Нет предыдущей фактической закупки этого материала у поставщика')
        elif not prior_terms:
            issues.append(prior.get('issue') or 'Последняя закупка требует подтверждения цены, условий и документа приёмки')
        elif terms:
            if not comparable(terms, prior_terms, D(str(item['quantity'])), prior_quantity):
                issues.append('НДС, доставка, условия оплаты или диапазоны партии несопоставимы')
            elif delivered > D(prior['delivered_unit_price']) or D(str(item['unit_price'])) > D(prior['unit_price']):
                issues.append('Цена товара или полная цена с доставкой выросла')
        if issues:
            problem('line_' + str(i), str(i + 1) + '. ' + item.get('material', 'Материал') + ': ' + '; '.join(issues))
        lines.append({'index': i, 'title': item.get('material'), 'material_id': item.get('warehouse_material_id'),
                      'unit': item.get('unit'), 'quantity': str(item['quantity']), 'unit_price': str(item['unit_price']),
                      'delivered_unit_price': str(delivered) if delivered is not None else None, 'previous': prior,
                      'targets': targets, 'issues': issues, 'offers': market, 'market_review_due': not bool(market)})
    from app.purchasing.budget import review as budget_review
    budgets = budget_review(db, row, allocations)
    for budget in budgets:
        for i, issue in enumerate(budget['issues']):
            problem(f"budget_{budget['order_id']}_{budget['line_index']}_{i}", issue)
    basis = {'budgets': budgets, 'order': order_basis(row), 'archived': row.archived, 'trust_id': trust_record.id if trust_record else 0,
             'price_id': price_record.id if price_record else 0, 'reasons': reasons,
             'lines': [{k: l[k] for k in ('index', 'previous', 'targets', 'issues')} for l in lines]}
    token = digest(basis)
    approved = latest(db, 'approval', row.supplier_id, row.id)
    valid_approval = bool(approved and not reasons and approved.content['fingerprint'] == token)
    people = {stage: governance.responsible(db, stage) for stage in ('accounting', 'director')}
    return {'order_id': row.id, 'supplier_id': row.supplier_id, 'supplier_name': supplier_row.name, 'currency': row.currency,
            'version': row.version, 'fingerprint': token, 'decision': 'auto_approved' if valid_approval else 'eligible' if not reasons else 'manual_review',
            'auto_approval_enabled': True, 'auto_payment_enabled': False, 'reasons': reasons, 'lines': lines, 'budgets': budgets,
            'approval_id': approved.id if valid_approval else None, 'previous_approval_id': approved.id if approved else None,
            'trust': {'id': trust_record.id if trust_record else 0, 'verified': trusted},
            'price': {'id': price_record.id if price_record else 0, 'verified': verified,
                      'request': price_record.content['request'] if price_record else None},
            'documents': [{'id': f.id, 'filename': f.filename} for f in documents(db, row.supplier_id).values()],
            'permissions': {stage: bool(user and person and user.id == person.id) for stage, person in people.items()},
            'people': {stage: person.full_name if person else 'Ответственный не назначен' for stage, person in people.items()},
            'payment_blockers': ['Банковская интеграция, лимиты, реквизиты, отсутствие дублей и условия оплаты проверяются отдельно. Автооплата не подключена.']}


def approve(db, row, body, user):
    old = replay(db, 'approval', row.supplier_id, row.id, body, user)
    if old:
        return old
    result = review(db, row, user)
    if body.fingerprint != result['fingerprint']:
        fail('Основания изменились. Обновите проверку.')
    if result['reasons']:
        fail('Не выполнены условия автосогласования')
    previous = latest(db, 'approval', row.supplier_id, row.id)
    if previous and previous.content['fingerprint'] == result['fingerprint']:
        return previous
    return record(db, 'approval', row.supplier_id, row.id, body, user,
                  fingerprint=result['fingerprint'], review=result)
