"""Read-only personal queue derived from current CRM tasks and deal ownership."""
from collections import Counter
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func
from app.crm.models import CRMEntity

BUCKETS = ('overdue', 'today', 'no_deadline', 'no_next_step', 'upcoming')
BUSINESS_ZONE = ZoneInfo('Europe/Moscow')


def deadline(value):
    if not value:
        return None
    try:
        result = datetime.fromisoformat(str(value))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result
    except (ValueError, TypeError):
        return None


def personal_queue(db, user, *, bucket=None, offset=0, limit=20, now=None):
    now = deadline(now or datetime.now(timezone.utc))
    today = now.astimezone(BUSINESS_ZONE).date()
    owned = db.query(CRMEntity).filter(
        CRMEntity.kind.in_(('task', 'deal')), CRMEntity.archived.is_(False),
        CRMEntity.payload['responsible_user_id'].as_integer() == user.id,
    ).all()
    tasks = [row for row in owned if row.kind == 'task' and not row.payload.get('completed')]
    deals = [row for row in owned if row.kind == 'deal' and not row.payload.get('is_unsorted')]
    parent_ids = {row.payload.get('entity_id') for row in tasks if row.payload.get('entity_id')}
    parents = {row.id: row for row in db.query(CRMEntity).filter(CRMEntity.id.in_(parent_ids))} if parent_ids else {}
    pending = [row for row in db.query(CRMEntity).filter(CRMEntity.kind == 'inquiry', CRMEntity.archived.is_(False),
               CRMEntity.payload['deal_id'].as_integer().in_(parent_ids)) if not row.payload.get('accepted_deal_id')] if parent_ids else []
    spam_links = {row.payload.get('deal_id') for row in pending if row.payload.get('is_spam')}
    normal_links = {row.payload.get('deal_id') for row in pending if not row.payload.get('is_spam')}
    hidden_spam = (spam_links - normal_links) & {row.id for row in parents.values() if row.kind == 'deal' and row.payload.get('is_unsorted')}
    stage_ids = {row.payload.get('stage_id') for row in deals if row.payload.get('stage_id')}
    stages = {row.id: row for row in db.query(CRMEntity).filter(CRMEntity.id.in_(stage_ids), CRMEntity.kind == 'stage')} if stage_ids else {}
    deal_ids = {row.id for row in deals}
    has_step = {parent for (parent,) in db.query(CRMEntity.payload['entity_id'].as_integer()).filter(
        CRMEntity.kind == 'task', CRMEntity.archived.is_(False),
        func.coalesce(CRMEntity.payload['completed'].as_boolean(), False).is_(False),
        CRMEntity.payload['entity_id'].as_integer().in_(deal_ids),
    )} if deal_ids else set()
    items = []
    for task in tasks:
        parent = parents.get(task.payload.get('entity_id'))
        # Spam is not a personal sales commitment. Other open tasks stay visible,
        # including tasks on archived cards, so a real assignment cannot disappear.
        if parent and ((parent.kind == 'inquiry' and parent.payload.get('is_spam')) or parent.id in hidden_spam):
            continue
        due = deadline(task.payload.get('due_at'))
        category = 'no_deadline' if due is None else 'overdue' if due < now else 'today' if due.astimezone(BUSINESS_ZONE).date() == today else 'upcoming'
        items.append({
            'id': task.id, 'kind': 'task', 'title': task.name, 'bucket': category,
            'due_at': due.astimezone(timezone.utc).isoformat() if due else None,
            'deadline_invalid': bool(task.payload.get('due_at') and due is None),
            'related': {'id': parent.id, 'kind': parent.kind, 'name': parent.name, 'archived': parent.archived} if parent else None,
        })
    for deal in deals:
        stage = stages.get(deal.payload.get('stage_id'))
        if deal.id in has_step or (stage and stage.payload.get('outcome') in ('won', 'lost')):
            continue
        items.append({'id': deal.id, 'kind': 'deal', 'title': deal.name, 'bucket': 'no_next_step',
                      'due_at': None, 'deadline_invalid': False, 'related': None})
    counts = Counter(item['bucket'] for item in items)
    items.sort(key=lambda item: (BUCKETS.index(item['bucket']), item['due_at'] or '', item['id']))
    selected = [item for item in items if not bucket or item['bucket'] == bucket]
    return {'checked_at': now.astimezone(timezone.utc).isoformat(), 'timezone': 'Europe/Moscow',
            'owner_id': user.id, 'counts': {key: counts[key] for key in BUCKETS},
            'total': len(selected), 'offset': offset, 'items': selected[offset:offset + limit]}
