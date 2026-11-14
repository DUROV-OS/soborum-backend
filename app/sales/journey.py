"""A derived order route: every completion comes from a linked source record."""
from collections import defaultdict
from datetime import datetime, timezone, date
from decimal import Decimal as D
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.common.module_access import Module
from app.core.deps import require_view
from app.costing import governance
from app.costing.integration import link_out
from app.costing.models import CostingEstimate, CostingRevision, CostingOrderLink
from app.costing.service import cost_access, estimate_query
from app.costing.technical_basis import fingerprint as digest
from app.db.session import get_db
from app.moysklad.recipes import ManufactureRun
from app.sales import service
from app.sales import journey_supply as supply
from app.sales.models import SalesShipment
from app.warehouse.models import WarehouseMaterial, StockMovement, StockMovementReason
from app.warehouse.reservation_service import allocations, dec, remaining

router = APIRouter()


def action(code, title, role, href=None, section=None):
    return {'code': code, 'title': title, 'responsible': role, 'href': href, 'section': section}


def checked_runs(db, recipe):
    """A run label alone is insufficient: validate the posted stock movements."""
    rows, invalid = [], False
    for run in db.query(ManufactureRun).filter_by(recipe_id=recipe.id).order_by(ManufactureRun.id):
        data = run.result or {}
        valid = bool(data and data.get('recipe_id') == recipe.id and data.get('recipe_version') == recipe.version
                     and data.get('recipe_snapshot') == recipe.content and data.get('issue_operation_id')
                     and data.get('receipt_operation_id'))
        for key, operation, sign, reason in [('inputs', 'issue_operation_id', -1, StockMovementReason.ISSUED_MANUAL),
                                              ('outputs', 'receipt_operation_id', 1, StockMovementReason.RECEIPT)]:
            expected, actual = defaultdict(D), defaultdict(D)
            for line in data.get(key, []):
                expected[line['id']] += sign * dec(line['quantity'])
            if data.get(operation):
                for movement in db.query(StockMovement).filter_by(operation_id=data[operation]):
                    if movement.reason != reason:
                        valid = False
                    actual[movement.warehouse_material_id] += dec(movement.delta)
            valid = valid and bool(expected) and expected == actual
        if valid:
            rows.append({'id': run.id, 'fraction': str(dec(run.parameters['multiplier'])),
                         'issue_operation_id': data['issue_operation_id'], 'receipt_operation_id': data['receipt_operation_id'],
                         'created_at': run.created_at, 'outputs': data['outputs']})
        else:
            invalid = True
    return rows, invalid


def route(db, order, user):
    rows = service.order_out(db, order)['lines']
    # A commercial employee sees only lines covered by their own estimate.
    # Warehouse/accounting staff and costing reviewers retain their module scope.
    operational = cost_access(user) or any(user.has_access(m) for m in (Module.WAREHOUSE, Module.ACCOUNTING, Module.PRODUCTION))
    permitted = None if operational else {e.id for e in estimate_query(db, user).filter_by(sales_order_id=order.id)}
    links = {l.line_index: l for l in db.query(CostingOrderLink).filter_by(order_id=order.id)}
    if permitted is not None:
        rows = [l for l in rows if l['order_line'] in links and links[l['order_line']].estimate_id in permitted]
        if not rows:
            service.fail('Маршрут заказа не найден', 404)
    active = supply.active_links(db)
    stock = {m.id: m for m in db.query(WarehouseMaterial)}
    holds = allocations(db, list(stock))
    free = {mid: max(D(0), dec(m.quantity_in_stock) - sum(holds.get(mid, {}).values(), D(0))) for mid, m in stock.items()}
    own = defaultdict(D)
    for hold in service.order_holds(db, order):
        own[hold.sales_line if hold.sales_order_id else hold.source_line] += remaining(hold)
    # Allocate a shared free balance only once across repeated order lines.
    coverage = {}
    for index, line in enumerate(service.order_out(db, order)['lines']):
        mid, need = line.get('material_id'), dec(line['remaining_quantity'])
        if mid in stock:
            held = min(need, own[index])
            take = min(max(D(0), need - held), free[mid])
            free[mid] -= take
            coverage[index] = (held, take)
    people = {stage: governance.responsible(db, stage) for stage in ('production', 'accounting', 'director')}
    roles = {stage: person.full_name if person else label for stage, person, label in
             [('production', people['production'], 'Начальник производства не назначен'),
              ('accounting', people['accounting'], 'Бухгалтер не назначен'),
              ('director', people['director'], 'Руководитель не назначен')]}
    shipments = db.query(SalesShipment).filter_by(order_id=order.id).order_by(SalesShipment.id).all()
    result = []
    for line in rows:
        index = line['order_line']
        required, link, recipe = supply.needs(db, order, index)
        estimate = db.get(CostingEstimate, link.estimate_id) if link else None
        revision = db.get(CostingRevision, link.revision_id) if link else None
        review = governance.review_state(db, estimate, revision) if estimate and revision else None
        fresh = bool(link and link_out(db, link, order)['fresh'] and estimate.version == revision.number and not estimate.archived)
        approved = bool(fresh and review and review['director_approved'])
        quote_href = f'/costing?tab=estimates&estimate={estimate.id}' if estimate else f'/costing?tab=estimates&order={order.id}&line={index}&new=1'
        quote = None if not estimate else {'id': estimate.id, 'number': estimate.number, 'revision': revision.number,
                 'fresh': fresh, 'approved': approved, 'stages': [
                     {'stage': stage, 'person': roles[stage], 'approved': bool(review[stage + '_approved']),
                      'signoff_id': review[stage + '_signoff_id']} for stage in ('production', 'accounting', 'director')]}
        runs, invalid_run = checked_runs(db, recipe) if recipe else ([], False)
        fraction = sum((dec(r['fraction']) for r in runs), D(0))
        recipe_valid = bool(recipe and not recipe.archived and not recipe.content.get('warnings') and required
                            and fraction <= 1 and not invalid_run)
        materials = []
        if recipe_valid:
            for mid, norm in required.items():
                material = stock.get(mid)
                need = max(D(0), norm * (1 - fraction))
                usable = min(need, free.get(mid, D(0))) if material and not material.is_archived else D(0)
                free[mid] = max(D(0), free.get(mid, D(0)) - usable)
                materials.append({'id': mid, 'title': material.title if material else f'Материал №{mid}',
                                  'unit': material.unit if material else '', 'norm': str(norm), 'remaining': str(need),
                                  'available': str(usable), 'shortage': str(need - usable)})
        purchase_links = [supply.state(db, a, order, recipe) for a in active if a.order_id == order.id and a.data['order_line'] == index]
        stale_supply = any(not a['fresh'] for a in purchase_links)
        held, available = coverage.get(index, (D(0), D(0)))
        left = dec(line['remaining_quantity'])
        short = max(D(0), left - held - available)
        material = stock.get(line.get('material_id'))
        stock_problem = bool(line.get('stock_tracked', True) and (not material or material.is_archived or dec(material.quantity_in_stock) < sum(holds.get(material.id, {}).values(), D(0))))
        documents = [{'id': s.id, 'number': s.number, 'status': s.status, 'historical': s.historical,
                      'quantity': str(sum((dec(l['quantity']) for l in s.lines if l.get('order_line') == index), D(0))),
                      'operation_id': s.operation_id, 'occurred_at': s.occurred_at} for s in shipments
                     if any(l.get('order_line') == index for l in s.lines)]
        warnings = []
        if not approved:
            warnings.append('Нет действующего полного согласования сметы этой строки.')
        if dec(line.get('opening_shipped')):
            warnings.append('Часть отгрузки перенесена из истории; новые складские операции для неё не создавались.')
        if recipe and not recipe_valid:
            warnings.append('Норматив или документы выпуска требуют сверки; выпуск не считается подтверждённым.')
        if stale_supply:
            warnings.append('Закупка или потребность изменилась. Снимите старое закрепление с причиной и выполните сверку заново.')
        if stock_problem:
            warnings.append('Складская карточка недоступна или остаток отрицательный. Нужна документальная сверка склада.')
        if order.archived or order.status == 'cancelled':
            next_step = action('stopped', 'Заказ в архиве' if order.archived else 'Заказ отменён', 'Менеджер')
        elif left == 0:
            next_step = action('shipped', 'Отгрузка завершена · сверить затраты и выручку', roles['accounting'],
                               f'/accounting?tab=costs&project={estimate.id}' if estimate else None, 'accounting')
        elif not approved:
            stage = review['next_stage'] if fresh and review else 'production'
            if stage == 'complete':
                stage = 'production'
            next_step = action('approval', 'Согласовать расчёт' if estimate else 'Создать смету по строке заказа',
                               roles[stage] if estimate else 'Менеджер', quote_href, 'estimates')
        elif order.status != 'confirmed':
            next_step = action('confirm', 'Подтвердить заказ', 'Менеджер', f'/sales?order={order.id}', 'sales')
        elif stock_problem or (recipe and not recipe_valid) or stale_supply:
            next_step = action('reconcile', 'Сверить основания и устранить расхождения', 'Специалист склада и снабжения', '/warehouse?tab=history', 'warehouse')
        elif not line.get('stock_tracked', True):
            next_step = action('service', 'Оформить выполнение услуги', 'Менеджер', f'/sales?order={order.id}&ship=1', 'sales')
        elif short == 0:
            next_step = action('ship' if held >= left else 'reserve', 'Создать отгрузку' if held >= left else 'Закрепить свободный остаток за заказом',
                               'Специалист склада и снабжения', f'/sales?order={order.id}' + ('&ship=1' if held >= left else ''), 'sales')
        elif recipe_valid and fraction < 1:
            shortages = any(dec(m['shortage']) > 0 for m in materials)
            next_step = action('purchase_materials' if shortages else 'manufacture', 'Обеспечить недостающие материалы' if shortages else 'Выпустить изделие по утверждённому нормативу',
                               'Специалист склада и снабжения' if shortages else roles['production'],
                               '/purchasing' if shortages else f'/recipes?recipe={recipe.id}', 'purchasing' if shortages else 'recipes')
        elif purchase_links:
            next_step = action('receive', 'Проверить приёмку и зарезервировать изделие', 'Специалист склада и снабжения', '/purchasing', 'purchasing')
        else:
            next_step = action('source', 'Выбрать обеспечение: закупка изделия или производственный норматив', roles['production'], quote_href, 'estimates')
        result.append({'index': index, 'title': line['title'], 'unit': line['unit'], 'quantity': line['quantity'],
                       'shipped': str(dec(line['shipped_quantity'])), 'remaining': str(left), 'reserved': str(held),
                       'free_for_line': str(available), 'shortage': str(short), 'stock_tracked': line.get('stock_tracked', True),
                       'estimate': quote, 'quote_href': quote_href, 'recipe': None if not recipe else {'id': recipe.id, 'title': recipe.title,
                       'valid': recipe_valid, 'completed_fraction': str(fraction), 'runs': runs}, 'materials': materials,
                       'purchases': purchase_links, 'shipments': documents, 'next': next_step, 'warnings': warnings,
                       'can_allocate': supply.can_allocate(user) and order.status == 'confirmed' and not order.archived and approved and left > 0 and bool(required)})
    return {'order_id': order.id, 'number': order.number, 'version': order.version, 'status': order.status,
            'archived': order.archived, 'expected_at': order.expected_at,
            'overdue': bool(order.expected_at and order.expected_at < datetime.now(ZoneInfo('Europe/Moscow')).date() and order.status != 'cancelled' and not order.archived and any(dec(l['remaining']) > 0 for l in result)),
            'lines': result, 'can_release': supply.can_allocate(user),
            'as_of': datetime.now(timezone.utc), 'source_hash': digest(result),
            'note': 'Закрепление закупки подтверждает план обеспечения. Приёмка, резерв и отгрузка подтверждаются своими документами; закрепление не выполняет эти операции.'}


@router.get('/orders/{id}/journey')
def journey(id: int, db: Session = Depends(get_db), user=Depends(require_view(Module.CLIENTS))):
    return route(db, service.get_order(db, id), user)
