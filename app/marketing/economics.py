"""Read-only, explicit receipt -> deal -> estimate -> accounting evidence.

Receipt cohorts and accounting periods are distinct. No last-touch inference,
campaign spend import, or new expense allocation policy is introduced here.
"""
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal

from app.accounting import actual
from app.clients.models import ClientLeadRequest
from app.costing.models import CostingEstimate
from app.costing.service import money
from app.crm.models import CRMEntity
from app.marketing.conversions import MOSCOW, aware, date_window, latest_reviews


def report(db, start, end, *, now=None):
    now = now or datetime.now(timezone.utc)
    begin, finish = date_window(start, end)
    requests = db.query(ClientLeadRequest).filter(
        ClientLeadRequest.created_at >= begin, ClientLeadRequest.created_at < finish,
    ).order_by(ClientLeadRequest.created_at, ClientLeadRequest.id).all()
    request_map = {r.id: r for r in requests}
    reviews = latest_reviews(db)
    deals = {d.id: d for d in db.query(CRMEntity).filter_by(kind='deal', archived=False)}
    projects = db.query(CostingEstimate).filter_by(archived=False).order_by(CostingEstimate.id).all()
    accounts = actual.overview(db)
    costs = {p['id']: p for p in accounts['projects']}
    order_lines = defaultdict(list)
    for p in projects:
        if p.sales_order_id is not None and p.sales_order_line is not None:
            order_lines[p.sales_order_id, p.sales_order_line].append(p.id)

    groups, request_groups = {}, {}
    for r in requests:
        utm = r.utm or {}
        key = tuple(str(utm.get(k) or '') for k in ('utm_source', 'utm_medium', 'utm_campaign'))
        group = groups.setdefault(key, {
            'source': key[0] or 'Не определён', 'medium': key[1], 'campaign': key[2],
            'requests': [], 'deal_ids': set(), 'projects': [],
        })
        review = reviews.get(r.id)
        group['requests'].append({'id': r.id, 'received_at': aware(r.created_at).isoformat(),
            'outcome': review.outcome if review else 'unreviewed',
            'has_visit_id': bool((r.tracking or {}).get('yclid') or (r.tracking or {}).get('client_id'))})
        request_groups[r.id] = group
    valid_deals = {}
    invalid_deal_ids = []
    for d in deals.values():
        r = request_map.get((d.payload or {}).get('client_request_id'))
        if not r:
            continue
        if d.payload.get('client_id') != r.client_id:
            invalid_deal_ids.append(d.id)
            continue
        valid_deals[d.id] = r
        request_groups[r.id]['deal_ids'].add(d.id)

    unresolved = []
    for p in projects:
        d = deals.get(p.crm_deal_id)
        r = valid_deals.get(p.crm_deal_id)
        if not d:
            unresolved.append({'id': p.id, 'reason': 'Нет связи с действующей сделкой CRM'})
            continue
        request_id = (d.payload or {}).get('client_request_id')
        if not request_id:
            unresolved.append({'id': p.id, 'reason': 'В сделке не указано исходное обращение сайта'})
            continue
        if not r:
            if request_id in request_map:
                unresolved.append({'id': p.id, 'reason': 'Клиент сделки не совпадает с клиентом обращения'})
            continue  # A receipt outside this cohort must not be counted in it.
        if p.client_id != r.client_id:
            unresolved.append({'id': p.id, 'reason': 'Клиент проекта не совпадает с клиентом обращения'})
            continue
        cost = costs[p.id]
        duplicate = len(order_lines[p.sales_order_id, p.sales_order_line]) > 1 if p.sales_order_id is not None and p.sales_order_line is not None else False
        complete = cost['complete'] and not duplicate
        documents = []
        for record in accounts['records']:
            allocations = [a for a in record['data']['allocations'] if a['project_id'] == p.id and a['category'] == 'advertising']
            if not allocations or record['review']['state'] == 'void':
                continue
            documents.append({'record_id': record['id'], 'version': record['version'],
                'amount': money(sum((Decimal(a['amount']) for a in allocations), Decimal(0))),
                'incurred_on': record['data']['incurred_on'], 'state': record['review']['state'],
                'file_id': record['proof']['file_id'], 'filename': record['proof']['filename'],
                'sha256': record['proof']['sha256'], 'source_hash': record['source_hash'],
                'locator': record['data']['evidence']['locator'],
                'allocation_basis': [a['locator'] for a in allocations],
                'tax_basis': record['data']['tax_basis'],
                'checked_by': record['review'].get('checked_by'), 'checked_at': record['review'].get('checked_at')})
        recognized = [x for x in documents if x['state'] == 'recognized']
        ad_cost = money(sum((Decimal(x['amount']) for x in recognized), Decimal(0))) if recognized or complete else None
        row = {'id': p.id, 'title': p.title, 'number': p.number, 'deal_id': d.id,
            'request_id': r.id, 'sales_order_id': p.sales_order_id,
            'recognized_advertising': ad_cost, 'advertising_documents': documents,
            'recognized_cost_subtotal': cost['recognized_subtotal'],
            'complete': complete, 'closure_state': 'duplicate_order_line' if duplicate else cost['closure']['state'],
            'net_revenue': cost.get('net_revenue') if complete else None,
            'actual_profit': cost['actual_profit'] if complete else None,
            'actual_margin_percent': cost['actual_margin_percent'] if complete else None,
            'below_target': cost.get('below_target') if complete else None,
            'source_hash': cost['source_hash'],
            'issues': list(dict.fromkeys(cost['issues'] + cost['closure']['issues'] +
                (['Несколько смет связаны с одной строкой заказа; устраните двойной учёт'] if duplicate else [])))}
        request_groups[r.id]['projects'].append(row)

    rows = []
    for group in groups.values():
        ids = {r['id'] for r in group['requests']}
        closed = [p for p in group['projects'] if p['complete']]
        group.update(deal_ids=sorted(group['deal_ids']),
            unique_contacts=len({request_map[i].client_id for i in ids}),
            qualified_unique_contacts=len({request_map[i].client_id for i in ids if reviews.get(i) and reviews[i].outcome == 'qualified'}),
            closed_project_count=len(closed),
            closed_project_profit=money(sum((Decimal(p['actual_profit']) for p in closed), Decimal(0))) if closed else None)
        rows.append(group)

    period_documents = []
    for record in accounts['records']:
        if not start.isoformat() <= record['data']['incurred_on'] <= end.isoformat() or record['review']['state'] == 'void':
            continue
        allocations = [a for a in record['data']['allocations'] if a['category'] == 'advertising']
        if allocations:
            period_documents.append({'record_id': record['id'], 'state': record['review']['state'],
                'amount': money(sum((Decimal(a['amount']) for a in allocations), Decimal(0))),
                'project_ids': sorted({a['project_id'] for a in allocations})})
    recognized = [d for d in period_documents if d['state'] == 'recognized']
    closed = [p for g in rows for p in g['projects'] if p['complete']]
    result = {'version': 'marketing-economics-v1', 'date_from': str(start), 'date_to': str(end),
        'timezone': 'Europe/Moscow', 'checked_at': now.isoformat(),
        'partial_today': end >= now.astimezone(MOSCOW).date(), 'currency': 'RUB',
        'scope': 'website_receipt_cohort', 'attribution_model': 'explicit_receipt_deal_project_links',
        'total_requests': len(requests), 'unique_contacts': len({r.client_id for r in requests}),
        'linked_projects': sum(len(g['projects']) for g in rows), 'closed_projects': len(closed),
        'closed_project_profit': money(sum((Decimal(p['actual_profit']) for p in closed), Decimal(0))) if closed else None,
        'period_advertising': {'recognized_amount': money(sum((Decimal(d['amount']) for d in recognized), Decimal(0))) if recognized else None,
            'documents': period_documents, 'pending_count': sum(d['state'] == 'pending' for d in period_documents),
            'stale_count': sum(d['state'] == 'stale' for d in period_documents)},
        'groups': rows, 'unlinked_projects_all_time': unresolved, 'invalid_deal_ids': invalid_deal_ids,
        'platform_spend': None, 'cost_per_qualified_contact': None, 'romi_percent': None,
        'limitations': [
            'Период отбирает обращения сайта. Результаты связанных проектов показаны за всё время на дату проверки.',
            'Источник — сохранённые UTM-метки обращения. Причинное влияние рекламы и атрибуция рекламной площадки не подтверждены.',
            'Расход за период — только признанные бухгалтером распределения статьи «Реклама проекта» по дате возникновения. Это не полный расход рекламы и не платежи.',
            'Прибыль закрытых проектов уже включает рекламу, комиссии, постоянные и переменные затраты. Повторно реклама не вычитается.',
            'Общий рекламный расход по сервисам и его распределение на кампании не сверены. CPQL и ROMI не рассчитываются.',
            'Уникальные контакты разных источников могут пересекаться; суммы контактов по строкам не равны общему числу.',
        ]}
    result['source_hash'] = actual.canonical({k: v for k, v in result.items() if k != 'checked_at'})
    return result
