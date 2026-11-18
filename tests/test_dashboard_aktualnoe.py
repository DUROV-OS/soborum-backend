"""Activity must remain traceable and must never invent a completion percentage."""
from datetime import datetime, timedelta, timezone
from app.ai import cache as ai_cache
from app.clients import service as client_service
from app.clients.models import ClientNote, ClientStage
from app.clients.schemas import ClientCreate
from app.common.module_access import Module
from app.cycle.models import CycleStatus
from app.dashboard.aktualnoe import CACHE_KEY, generate_aktualnoe


def seed(db, name='Клиент'):
    client = client_service.create_client(db, ClientCreate(full_name=name, phone='+70000000000', email='test@example.com'))
    note = ClientNote(client_id=client.id, author_id=1, text='Заметка', created_at=datetime.now(timezone.utc)-timedelta(hours=1))
    db.add(note); db.commit()
    return client, note


def test_activity_has_real_record_references_and_no_percentage(db, make_user):
    user = make_user(Module.CYCLE)
    client, note = seed(db)
    result = generate_aktualnoe(db,user)
    item = result.items[0]
    assert result.source == 'database' and result.progress_basis == 'not_calculated'
    # Creating a client also creates a stage-task event; the explicit note adds one.
    assert item.percent is None and item.source_hash and item.recent_events==2
    source = next(s for s in item.sources if s.entity=='client_notes')
    assert source.record_id==note.id and source.field=='created_at'
    assert item.cycle_id==client.cycle_id
    assert ai_cache.get(db,CACHE_KEY) is None


def test_legacy_ai_cache_is_ignored_and_changes_are_visible_immediately(db, make_user):
    user=make_user(Module.CYCLE)
    client,note=seed(db)
    first=generate_aktualnoe(db,user)
    ai_cache.set(db,CACHE_KEY,{'items':[{'percent':99,'stage':'Всё готово'}]},datetime.now(timezone.utc))
    client.stage=ClientStage.DISCUSSION;db.commit()
    second=generate_aktualnoe(db,user)
    assert second.items[0].stage=='Обсуждение' and second.items[0].percent is None
    assert second.items[0].source_hash!=first.items[0].source_hash
    db.delete(note);db.commit()
    third=generate_aktualnoe(db,user)
    assert not any(s.entity=='client_notes' for s in third.items[0].sources)


def test_completed_cycle_and_future_activity_are_not_recent_work(db, make_user):
    user=make_user(Module.CYCLE)
    client,note=seed(db)
    before=generate_aktualnoe(db,user).items[0].recent_events
    note.created_at=datetime.now(timezone.utc)+timedelta(days=1);db.commit()
    result=generate_aktualnoe(db,user)
    assert result.items[0].recent_events==before-1
    assert not any(s.entity=='client_notes' for s in result.items[0].sources)
    client.cycle.status=CycleStatus.COMPLETED;db.commit()
    assert generate_aktualnoe(db,user).items==[]


def test_no_access_no_activity(db,make_user):
    user=make_user(Module.CLIENTS);seed(db)
    assert generate_aktualnoe(db,user).items==[]
