"""Current activity from identifiable database records; activity is not progress."""
from datetime import datetime, timedelta, timezone
from sqlalchemy import or_
from sqlalchemy.orm import Session
from app.ai.provenance import fingerprint
from app.common.module_access import Module
from app.dashboard.schemas import ActivitySource, AktualnoeItem, AktualnoeOut
from app.clients.models import ClientNote, ClientStage
from app.cycle.models import Cycle, CycleStatus
from app.installation.models import InstallationStage
from app.production.models import Production, ProductionBlock
from app.tasks.models import Task, TaskLinkType, TaskStageEvent

RECENT_WINDOW = timedelta(days=21)
CACHE_KEY = 'dashboard_aktualnoe'  # Legacy cache is deliberately never consumed.
CLIENT_STAGE_LABEL = {
    ClientStage.LEAD: 'Лид', ClientStage.DISCUSSION: 'Обсуждение',
    ClientStage.SITE_VISIT: 'Визит на объект', ClientStage.APPROVAL: 'Одобрение в банке',
    ClientStage.PAYMENT: 'Договор / аванс', ClientStage.POSTPAYMENT: 'Дом в производстве',
    ClientStage.ACCEPTANCE: 'Приёмка', ClientStage.COMPLETED: 'Завершён',
}
INSTALLATION_STAGE_LABEL = {InstallationStage.DELIVERY: 'Доставка дома',
    InstallationStage.INSTALLATION: 'Монтаж на участке', InstallationStage.FOLLOWUP: 'После монтажа'}


def _aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _stage_of(cycle, client):
    if cycle.status == CycleStatus.COMPLETED:
        return 'Цикл завершён', None
    if cycle.status == CycleStatus.PRODUCTION:
        return 'Производство дома', None
    if cycle.status == CycleStatus.INSTALLATION:
        return (INSTALLATION_STAGE_LABEL.get(cycle.installation.stage, 'Монтаж') if cycle.installation else 'Монтаж'), None
    return CLIENT_STAGE_LABEL.get(client.stage, 'Работа с клиентом'), None


def collect_cycle_activity(db: Session):
    now = datetime.now(timezone.utc)
    since = now - RECENT_WINDOW
    items = []
    for cycle in db.query(Cycle).all():
        client = cycle.client
        if client is None or cycle.status == CycleStatus.COMPLETED:
            continue
        stage, _ = _stage_of(cycle, client)
        sources = []
        for field, label in [('created_at', 'Карточка создана'), ('documents_locked_at', 'Отметка фиксации документов'),
                             ('payment_locked_at', 'Отметка оплаты в карточке'), ('balance_paid_at', 'Отметка оплаты остатка')]:
            value = getattr(client, field)
            if value is not None and _aware(value) <= now:
                sources.append(ActivitySource(entity='clients', record_id=client.id, field=field, label=label, at=_aware(value)))
        notes = db.query(ClientNote.id, ClientNote.created_at).filter(ClientNote.client_id == client.id, ClientNote.created_at <= now).all()
        blocks = db.query(ProductionBlock.id).join(Production, Production.id == ProductionBlock.production_id).filter(Production.cycle_id == cycle.id)
        events = db.query(TaskStageEvent.id, TaskStageEvent.created_at).join(Task, Task.id == TaskStageEvent.task_id).filter(
            or_((Task.link_type == TaskLinkType.CLIENT_STAGE) & (Task.link_id == client.id), Task.block_id.in_(blocks)),
            TaskStageEvent.created_at <= now).all()
        for entity, label, rows in [('client_notes', 'Заметка по клиенту', notes), ('task_stage_events', 'Изменение статуса задачи', events)]:
            if rows:
                record = max(rows, key=lambda r: (_aware(r.created_at), r.id))
                sources.append(ActivitySource(entity=entity, record_id=record.id, field='created_at', label=label, at=_aware(record.created_at)))
        if not sources:
            continue
        sources.sort(key=lambda s: (s.at, s.entity, s.record_id), reverse=True)
        count = sum(1 for record in [*notes, *events] if since <= _aware(record.created_at) <= now)
        payload = {'cycle_id':cycle.id, 'client_id':client.id, 'client_name':client.full_name, 'stage':stage,
                   'recent_events':count, 'sources':[s.model_dump(mode='json') for s in sources]}
        items.append(AktualnoeItem(cycle_id=cycle.id, client_name=client.full_name, stage=stage,
                     phrase=sources[0].label, last_activity=sources[0].at, recent_events=count,
                     sources=sources, source_hash=fingerprint(payload)))
    items.sort(key=lambda item:(item.recent_events, item.last_activity, item.cycle_id), reverse=True)
    return items


def generate_aktualnoe(db: Session, user, force=False):
    items = collect_cycle_activity(db)[:3] if user.has_access(Module.CYCLE) else []
    return AktualnoeOut(generated_at=datetime.now(timezone.utc), items=items)
