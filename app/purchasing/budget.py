"""Conservative procurement commitments, not an actual-profit calculation.

An allocation is a plan. An immutable approval is an obligation: removing the
allocation or archiving its order cannot silently recover the approved budget.
All arithmetic stays decimal; unknown evidence is a blocker, never a zero cost.
"""
from collections import defaultdict
from decimal import Decimal as D, ROUND_CEILING

from app.accounting.models import SupplierOrder, MoneyMovement
from app.costing.models import CostingEstimate, CostingOrderLink, CostingRevision
from app.purchasing.control_models import PurchaseControlRecord as Record
from app.purchasing.models import PurchaseReceipt
from app.sales.models import SalesEvent


def money(value):
    return str(value.quantize(D('.01'), rounding=ROUND_CEILING))


def projection(bom, commitments, cost, price):
    """Reserve unpurchased quantities at the approved weighted BOM price."""
    spent, quantities = D(0), defaultdict(D)
    issues = []
    for item in commitments:
        quantities[item['material_id']] += D(item['quantity'])
        spent += D(item['amount'])
    reserved = D(0)
    for mid, material in bom.items():
        if quantities[mid] > material['quantity']:
            issues.append('Закупки превышают полную норму материала №' + str(mid))
        reserved += max(D(0), material['quantity'] - quantities[mid]) * material['amount'] / material['quantity']
    if set(quantities) - set(bom):
        issues.append('В закупках есть материал вне утверждённой сметы')
    approved_materials = sum((x['amount'] for x in bom.values()), D(0))
    projected_materials = spent + reserved
    projected_cost = cost - approved_materials + projected_materials
    if projected_materials > approved_materials:
        issues.append('Закупки с доставкой и резервом оставшихся материалов превышают смету')
    if price <= 0 or projected_cost > price * D('.65'):
        issues.append('Прогнозная маржа ниже обязательных 35%')
    return {'approved_materials': money(approved_materials), 'committed': money(spent),
            'remaining_material_reserve': money(reserved), 'other_expense_reserve': money(cost-approved_materials),
            'available': money(approved_materials-projected_materials), 'projected_cost': money(projected_cost),
            'sale_price': money(price), 'forecast_margin_percent': str((price-projected_cost)/price*100) if price > 0 else None,
            'issues': issues}


def review(db, current, allocations):
    from app.purchasing import controls as c
    from app.sales import journey_supply as supply
    from app.sales.models import SalesOrder
    from app.accounting import actual

    events = {a.id: a for a in db.query(SalesEvent).filter_by(kind='supply_allocated')}
    active = {a.id for a in allocations}
    # One latest immutable approval per supplier order, including old releases
    # which predate this budget feature. The old review already freezes targets.
    approvals = {}
    for record in db.query(Record).filter_by(kind='approval').order_by(Record.id):
        approvals[record.order_id] = record
    held = defaultdict(list)
    targets = set()
    for record in approvals.values():
        for line in record.content['review']['lines']:
            for target in line['targets']:
                event = events.get(target['allocation_id'])
                if not event:
                    continue  # immutable FK-less evidence is checked below
                key = (record.order_id, line['index'], event.order_id, event.data['order_line'])
                held[key].append((line, target))
                if record.order_id == current.id:
                    targets.add(key[2:])
    for a in allocations:
        if a.data['supplier_order_id'] == current.id:
            targets.add((a.order_id, a.data['order_line']))
    result = []
    for oid, index in sorted(targets):
        issues = []
        uncertain = False
        order = db.get(SalesOrder, oid)
        link = db.query(CostingOrderLink).filter_by(order_id=oid, line_index=index).first()
        revision = db.get(CostingRevision, link.revision_id) if link else None
        estimate = db.get(CostingEstimate, link.estimate_id) if link else None
        if not order or not revision or not estimate or estimate.archived:
            result.append({'order_id': oid, 'line_index': index, 'issues': ['Нет связи с действующей утверждённой сметой; обязательства требуют сверки']})
            continue
        snapshot = revision.snapshot
        bom = defaultdict(lambda: {'quantity': D(0), 'amount': D(0), 'unit': None})
        for line in snapshot.get('bom', []):
            source = snapshot['sources']['materials'].get(str(line['material_id']), {})
            mid = source.get('warehouse_material_id')
            if not mid or line.get('price') is None:
                uncertain = True
                issues.append('Материал сметы не сопоставлен со складом или не имеет подтверждённой цены')
                continue
            quantity = D(line['quantity'])
            if quantity <= 0:
                continue
            bom[mid]['quantity'] += quantity
            bom[mid]['amount'] += quantity * D(line['price'])
            if bom[mid]['unit'] and bom[mid]['unit'] != line['unit']:
                issues.append('Единицы одного материала в смете различаются')
            bom[mid]['unit'] = line['unit']
        selected = defaultdict(list)
        for a in allocations:
            if a.order_id == oid and a.data['order_line'] == index:
                selected[(a.data['supplier_order_id'], a.data['supplier_line'], oid, index)].append(a)
        keys = sorted(set(selected) | {key for key in held if key[2:] == (oid, index)})
        commitments = []
        for key in keys:
            sid, si, _, _ = key
            purchase = db.get(SupplierOrder, sid)
            saved = held.get(key, [])
            rows = selected.get(key, [])
            old_qty = sum((D(t['quantity']) for _, t in saved), D(0))
            old_amount = sum((D(t['quantity']) * D(l['delivered_unit_price']) for l, t in saved
                              if l.get('delivered_unit_price') is not None), D(0))
            # A later approval also carries forward its own older obligations.
            # Otherwise a lower replacement price could erase the first promise.
            approved = approvals.get(sid)
            for budget in approved.content['review'].get('budgets', []) if approved else []:
                if (budget['order_id'], budget['line_index']) != (oid, index):
                    continue
                for obligation in budget.get('commitments', []):
                    if (obligation['order_id'], obligation['line_index']) == (sid, si):
                        old_qty = max(old_qty, D(obligation['quantity']))
                        old_amount = max(old_amount, D(obligation['amount']))
            if any(l.get('delivered_unit_price') is None for l, _ in saved):
                uncertain = True
                issues.append('У прежнего согласования закупки №'+str(sid)+' нет полной цены')
            qty = sum((D(a.data['quantity']) for a in rows), D(0))
            amount = D(0)
            material = rows[0].data['material_id'] if rows else saved[0][0]['material_id']
            if rows:
                proof, valid = c.price_state(db, purchase) if purchase else (None, False)
                if not purchase or purchase.archived or si >= len(purchase.items) or not valid:
                    uncertain = True
                    issues.append('Перепроверьте документ и состояние закупки №'+str(sid))
                else:
                    item = purchase.items[si]
                    terms = next((x for x in proof.content['request']['lines'] if x['index'] == si), None)
                    if not terms or purchase.currency != 'RUB' or item.get('unit') != bom.get(material, {}).get('unit'):
                        uncertain = True
                        issues.append('Не подтверждены валюта, единица или доставка закупки №'+str(sid))
                    else:
                        amount = qty * (D(str(item['unit_price'])) + D(terms['delivery_per_unit']))
                    if any(a.data['supply_basis'] != supply.supply_basis(purchase, si) for a in rows):
                        issues.append('Изменились условия закреплённой закупки №'+str(sid))
            if saved and ({t['allocation_id'] for _, t in saved} - active or not purchase or purchase.archived):
                issues.append('Закупка №'+str(sid)+' согласована ранее. Снятие связи или архив не освобождают бюджет; нужна сверка обязательства')
            if saved and any(l['material_id'] != material for l, _ in saved):
                issues.append('Изменился материал ранее согласованной закупки №'+str(sid))
            commitments.append({'order_id': sid, 'line_index': si, 'material_id': material,
                                'quantity': str(max(qty, old_qty)), 'amount': str(max(amount, old_amount))})
        # An unapproved but paid/received allocation is also not a free budget.
        for event in events.values():
            d = event.data
            if event.order_id != oid or d['order_line'] != index or event.id in active:
                continue
            sid = d['supplier_order_id']
            if (sid, d['supplier_line'], oid, index) in held:
                continue
            purchase = db.get(SupplierOrder, sid)
            paid = db.query(MoneyMovement).filter_by(supply_id=sid).all()
            receipts = db.query(PurchaseReceipt).filter_by(order_id=sid, status='posted').count()
            if receipts or (purchase and purchase.opening_paid) or any(str(getattr(p.status, 'value', p.status)) != 'cancelled' for p in paid):
                issues.append('У снятой закупки №'+str(sid)+' есть приёмка или платёж. Нужна сверка с проектом')
        cost, price = snapshot.get('result', {}).get('cost'), snapshot.get('result', {}).get('price')
        if cost is None or price is None or not bom:
            issues.append('Нет полного утверждённого бюджета проекта')
            calculated = {}
        else:
            calculated = projection(bom, commitments, D(cost), D(price))
            issues += calculated.pop('issues')
        # Actual materials already issued from these purchases are not added a
        # second time. Unmatched lots and unrecognized facts require accounting.
        actual_data = actual.overview(db, link.estimate_id, with_closures=False)
        actual_project = actual_data['projects'][0]
        issues += [issue for issue in actual_project['issues'] if not issue.startswith('Нет признанных фактических затрат по статье')]
        issued = defaultdict(D)
        for row in actual_data['records']:
            if row['review']['state'] == 'void':
                continue
            if row['review']['state'] != 'recognized':
                issues.append('Есть непризнанные фактические затраты проекта; нужна проверка бухгалтерии')
            if row['data']['kind'] == 'material':
                receipt = db.get(PurchaseReceipt, row['data']['receipt_id'])
                ri = row['data']['receipt_line']
                source_line = receipt.lines[ri] if receipt and 0 <= ri < len(receipt.lines) else {}
                if not receipt or not any(x['order_id'] == receipt.order_id and x['line_index'] == source_line.get('order_line') for x in commitments):
                    issues.append('Фактическая выдача не сверена с закупками проекта; нужна проверка партии')
                elif row['review']['state'] == 'recognized':
                    issued[(receipt.order_id, source_line['order_line'])] += D(row['net_amount'])
        for commitment in commitments:
            if issued[(commitment['order_id'], commitment['line_index'])] > D(commitment['amount']):
                issues.append('Признанная стоимость выданных материалов превышает учтённое обязательство закупки')
        nonmaterials = D(actual_project['recognized_subtotal']) - D(actual_project['categories'].get('materials', '0'))
        if calculated and nonmaterials > D(calculated['other_expense_reserve']):
            issues.append('Признанные расходы сверх материалов превышают сохранённый резерв сметы')
            uncertain = True
        if uncertain and calculated:
            for key in ('committed', 'available', 'projected_cost', 'forecast_margin_percent'):
                calculated[key] = None
        result.append({'order_id': oid, 'line_index': index, 'number': order.number, 'estimate_id': link.estimate_id,
                       'revision_id': revision.id, **calculated, 'commitments': commitments,
                       'actual_source_hash': actual_project['source_hash'], 'issues': list(dict.fromkeys(issues))})
    return result
