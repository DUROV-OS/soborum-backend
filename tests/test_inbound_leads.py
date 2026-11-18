from datetime import datetime, timedelta, timezone
import pytest
from app.common.module_access import Module
from app.crm.models import CRMEntity, CRMEvent
from app.crm.chat_models import CRMChatLink
from app.crm import max_intake
from app.crm import chat_links
from app.notifications.models import Notification
from app.telephony import routing, service
from app.telephony.models import PhoneCall, PhoneRoutingConfig, PhoneRoutingMemory, MAXInboundMessage


@pytest.fixture
def intake(db, make_user):
    first, second, actor = make_user(Module.CLIENTS), make_user(Module.CLIENTS), make_user(admin=True)
    first.job_role = second.job_role = 'manager'
    actor.job_role = 'director'
    pipeline = CRMEntity(kind='pipeline', name='Продажи', payload={'purpose': 'sales'})
    db.add(pipeline); db.flush()
    stage = CRMEntity(kind='stage', name='Новые заявки', payload={'pipeline_id': pipeline.id, 'outcome': 'open'})
    db.add(stage); db.flush()
    routing.save_config(db, routing.Configuration(version=1, auto_create_leads=True, max_incoming_enabled=True,
        lead_stage_id=stage.id, members=[routing.Member(user_id=first.id, pbx_user='101'), routing.Member(user_id=second.id, pbx_user='102')]), actor)
    db.get(PhoneRoutingConfig, 1).max_leads_started_at = datetime.now(timezone.utc) - timedelta(seconds=60)
    db.commit()
    return first, second, stage


def call(db, **fields):
    return service.ingest(db, {'cmd': 'event', 'type': 'INCOMING', 'phone': '79001112233',
        'diversion': '79066773555', 'callid': 'first-call', **fields})


def message(db, *, number='79001112233', message_id='m1', **fields):
    result = max_intake.ingest(db, {'account_id': '1', 'sender_id': '2', 'chat_id': 3, 'phone': number, 'name': 'Клиент'},
        {'id': message_id, 'sender': 2, 'time': int(datetime.now(timezone.utc).timestamp() * 1000), 'text': 'Нужен расчёт', **fields})
    db.commit()
    return result


def test_incoming_creates_new_stage_contact_deal_and_followup_without_unsorted(db, intake):
    first, _, stage = intake
    call(db)
    deal = db.query(CRMEntity).filter_by(kind='deal').one()
    assert deal.payload['stage_id'] == stage.id and not deal.payload['is_unsorted']
    assert deal.payload['responsible_user_id'] == first.id
    assert db.query(CRMEntity).filter_by(kind='inquiry').count() == 0
    assert db.query(CRMEntity).filter_by(kind='task').one().payload['entity_id'] == deal.id
    assert db.query(PhoneCall).one().deal_id == deal.id
    for idx, phone in enumerate(['+7 (900) 111-22-33', '89001112233', '9001112233']):
        call(db, phone=phone, callid='repeat-' + str(idx))
    assert db.query(CRMEntity).filter_by(kind='deal').count() == 1
    assert db.query(CRMEntity).filter_by(kind='contact').count() == 1
    assert db.get(PhoneRoutingConfig, 1).cursor == 1


def test_missed_history_and_idless_incoming_are_idempotent(db, intake):
    call(db, callid='')
    assert db.query(PhoneCall).count() == 0
    for _ in range(2):
        call(db, cmd='history', type='in', status='Missed', duration='0', start='20261109T093000Z')
    assert db.query(PhoneCall).count() == db.query(CRMEntity).filter_by(kind='deal').count() == 1


def test_existing_contact_gets_a_new_lead_without_a_duplicate_contact(db, intake):
    first, _, stage = intake
    contact = CRMEntity(kind='contact', name='Existing customer', payload={
        'phone': '+7 (900) 111-22-33', 'responsible_user_id': first.id})
    db.add(contact); db.commit()
    call(db)
    deal = db.query(CRMEntity).filter_by(kind='deal').one()
    assert deal.payload['contact_ids'] == [contact.id]
    assert deal.payload['responsible_user_id'] == first.id
    assert deal.payload['stage_id'] == stage.id and not deal.payload['is_unsorted']
    assert db.query(CRMEntity).filter_by(kind='contact').count() == 1
    assert db.query(PhoneCall).one().deal_id == deal.id


def test_live_call_prefers_the_only_open_lead_and_archive_keeps_the_choice(db, intake):
    call(db)
    current = db.query(CRMEntity).filter_by(kind='deal').one()
    closed_stage = CRMEntity(kind='stage', name='Won', payload={
        'pipeline_id': current.payload['pipeline_id'], 'outcome': 'won'})
    db.add(closed_stage); db.flush()
    closed = CRMEntity(kind='deal', name='Previous deal', payload={
        **current.payload, 'stage_id': closed_stage.id})
    db.add(closed); db.commit()
    call(db, callid='second-call')
    row = db.query(PhoneCall).filter_by(external_id='second-call').one()
    assert row.deal_id == current.id and row.deal_match_status == 'current'
    service.ingest(db, {'cmd': 'history', 'type': 'in', 'phone': '79001112233',
        'callid': 'second-call', 'start': '20261114T090000Z', 'duration': 42,
        'status': 'Success', 'link': 'https://record.example.test/call.mp3'}, remember=False)
    assert row.deal_id == current.id
    assert row.recording_url and row.processing == 'queued'
    assert db.query(CRMEntity).filter_by(kind='deal').count() == 2


def test_call_event_is_enriched_by_history_without_duplicate_timeline_items(db, intake):
    call(db)
    call(db)
    call(db, type='ACCEPTED')
    service.ingest(db, {'cmd': 'history', 'type': 'in', 'phone': '79001112233',
        'diversion': '79066773555', 'callid': 'first-call', 'start': '20261114T090000Z',
        'duration': 42, 'status': 'Success', 'link': 'https://record.example.test/call.mp3'})
    row = db.query(PhoneCall).one()
    event = db.query(CRMEvent).filter_by(action='phone_call').one()
    assert event.entity_id == row.deal_id
    assert event.details['call_id'] == row.id and event.details['duration'] == 42
    assert event.details['status'] == 'Success'
    assert event.details['note_type'] == 'call_in'
    assert event.details['params'] == {'phone': '+79001112233', 'duration': 42,
                                      'call_status': 'состоялся', 'service': 'МегаФон'}
    assert 'record.example.test' not in str(event.details)
    service.ingest(db, {'cmd': 'history', 'type': 'in', 'phone': '79001112233',
        'callid': 'first-call', 'start': '20261114T090000Z', 'duration': 42,
        'status': 'success'}, remember=False)
    assert 'состоялся' in event.details['text']
    assert db.query(CRMEvent).filter_by(action='phone_call').count() == 1
    call(db, callid='another-call', phone='8 (900) 111-22-33')
    assert db.query(CRMEvent).filter_by(action='phone_call').count() == 2
    assert db.query(CRMEntity).filter_by(kind='deal').count() == 1


def test_returning_customer_after_closed_deal_gets_a_fresh_lead(db, intake):
    call(db)
    previous = db.query(CRMEntity).filter_by(kind='deal').one()
    closed_stage = CRMEntity(kind='stage', name='Won', payload={
        'pipeline_id': previous.payload['pipeline_id'], 'outcome': 'won'})
    db.add(closed_stage); db.flush()
    previous.payload = {**previous.payload, 'stage_id': closed_stage.id}
    db.commit()
    call(db, callid='returning-call')
    new = db.query(PhoneCall).filter_by(external_id='returning-call').one()
    assert new.deal_id != previous.id
    assert db.get(CRMEntity, new.deal_id).payload['stage_id'] == intake[2].id
    assert previous.payload['stage_id'] == closed_stage.id
    call(db, callid='returning-call')
    assert db.query(CRMEntity).filter_by(kind='deal').count() == 2
    assert db.query(CRMEntity).filter_by(kind='contact').count() == 1


def test_two_open_leads_require_manual_binding(db, intake):
    call(db)
    first = db.query(CRMEntity).filter_by(kind='deal').one()
    db.add(CRMEntity(kind='deal', name='Another open deal', payload=dict(first.payload)))
    db.commit()
    call(db, callid='ambiguous-call')
    row = db.query(PhoneCall).filter_by(external_id='ambiguous-call').one()
    assert row.deal_id is None and row.deal_match_status == 'ambiguous'
    assert db.query(CRMEntity).filter_by(kind='deal').count() == 2


def test_contact_and_company_linked_to_one_deal_are_not_ambiguous_customers(db, intake):
    first, _, stage = intake
    contact = CRMEntity(kind='contact', name='Customer contact', payload={
        'phone': '79001112233', 'responsible_user_id': first.id})
    company = CRMEntity(kind='company', name='Customer company', payload={
        'phone': '79001112233', 'responsible_user_id': first.id})
    db.add_all([contact, company]); db.flush()
    deal = CRMEntity(kind='deal', name='Current lead', payload={
        'pipeline_id': stage.payload['pipeline_id'], 'stage_id': stage.id,
        'contact_ids': [contact.id], 'company_id': company.id, 'responsible_user_id': first.id})
    db.add(deal); db.commit()
    call(db)
    row = db.query(PhoneCall).one()
    assert row.deal_id == deal.id and row.match_status == 'matched'
    assert row.crm_entity_id == contact.id
    assert db.query(CRMEntity).filter_by(kind='deal').count() == 1
    assert db.query(CRMEvent).filter_by(action='phone_call', entity_id=deal.id).count() == 1


def test_unrelated_company_and_contact_with_same_phone_still_need_manual_binding(db, intake):
    first, _, stage = intake
    contact = CRMEntity(kind='contact', name='Person', payload={'phone': '79001112233'})
    company = CRMEntity(kind='company', name='Different company', payload={'phone': '79001112233'})
    db.add_all([contact, company]); db.flush()
    db.add(CRMEntity(kind='deal', name='Person lead', payload={
        'pipeline_id': stage.payload['pipeline_id'], 'stage_id': stage.id,
        'contact_ids': [contact.id], 'responsible_user_id': first.id}))
    db.commit()
    call(db)
    row = db.query(PhoneCall).one()
    assert row.deal_id is None and row.match_status == 'ambiguous'
    assert db.query(CRMEntity).filter_by(kind='deal').count() == 1


@pytest.mark.parametrize('fields', [dict(diversion='79001114455'), dict(diversion=''), dict(type='OUTGOING'), dict(direction='out'), dict(cmd='contact', callid='')])
def test_only_identified_main_line_incoming_creates_a_lead(db, intake, fields):
    call(db, **fields)
    assert db.query(CRMEntity).filter_by(kind='deal').count() == 0


def test_live_contact_callback_creates_intake_before_any_manager_event(db, intake):
    first, _, stage = intake
    config = db.get(PhoneRoutingConfig, 1)
    config.enabled = config.pbx_setup_confirmed = True
    db.commit()
    body = {'cmd': 'contact', 'phone': '79001112233', 'diversion': '79066773555', 'callid': 'pbx-arrival'}
    response = service.ingest(db, body)
    assert response['responsible'] == '101'
    row = db.query(PhoneCall).one()
    deal = db.query(CRMEntity).filter_by(kind='deal').one()
    assert row.external_id == 'pbx-arrival' and row.direction == 'in'
    assert row.deal_id == deal.id and row.status == 'INCOMING'
    assert deal.payload['stage_id'] == stage.id and deal.payload['responsible_user_id'] == first.id
    assert not deal.payload['is_unsorted']
    assert db.query(CRMEvent).filter_by(action='phone_call', entity_id=deal.id).count() == 1
    assert db.query(CRMEntity).filter_by(kind='task').count() == 1
    assert config.cursor == 1


def test_live_contact_retry_and_later_missed_history_enrich_one_card(db, intake):
    body = {'cmd': 'contact', 'phone': '79001112233', 'diversion': '79066773555', 'callid': 'pbx-arrival'}
    for _ in range(2):
        service.ingest(db, body)
    call(db, callid='pbx-arrival')
    service.ingest(db, {**body, 'cmd': 'history', 'type': 'in', 'status': 'missed',
                       'start': '20261118T071946Z', 'duration': 0})
    row = db.query(PhoneCall).one()
    event = db.query(CRMEvent).filter_by(action='phone_call').one()
    assert row.status == 'missed' and row.processing == 'no_recording'
    assert event.details['status'] == 'missed' and event.entity_id == row.deal_id
    assert db.query(CRMEntity).filter_by(kind='deal').count() == 1
    assert db.query(CRMEntity).filter_by(kind='contact').count() == 1
    assert db.get(PhoneRoutingConfig, 1).cursor == 1


@pytest.mark.parametrize('fields', [dict(callid=''), dict(diversion=''), dict(diversion='79066778555'),
                                     dict(direction='out'), dict(type='OUTGOING')])
def test_contact_name_lookups_and_other_lines_never_create_intake(db, intake, fields):
    service.ingest(db, {'cmd': 'contact', 'phone': '79001112233', 'diversion': '79066773555',
                        'callid': 'pbx-arrival', **fields})
    assert db.query(PhoneCall).count() == 0
    assert db.query(CRMEntity).filter_by(kind='deal').count() == 0


def test_live_contact_callback_respects_disabled_automatic_leads(db, intake):
    db.get(PhoneRoutingConfig, 1).auto_create_leads = False
    db.commit()
    service.ingest(db, {'cmd': 'contact', 'phone': '79001112233', 'diversion': '79066773555', 'callid': 'pbx-arrival'})
    assert db.query(PhoneCall).count() == db.query(CRMEntity).filter_by(kind='deal').count() == 0


def test_phone_then_max_share_the_same_card_owner_and_history(db, intake):
    call(db)
    deal = db.query(CRMEntity).filter_by(kind='deal').one()
    before = dict(deal.payload)
    message(db)
    message(db)
    assert db.query(CRMChatLink).count() == 0
    assert max_intake.pending(db, 3)['suggested']['id'] == deal.id
    assert db.query(Notification).count() == 3
    chat_links.create_link(db, chat_links.LinkFromMax(max_chat_id=3, entity_id=deal.id), intake[0])
    message(db, message_id='m2')
    assert db.query(CRMEntity).filter_by(kind='deal').count() == 1
    assert db.query(CRMChatLink).one().entity_id == deal.id
    assert db.query(MAXInboundMessage).count() == 2
    assert db.query(CRMEvent).filter_by(action='max_message_received', entity_id=deal.id).count() == 2
    assert deal.payload == before
    from app.crm.service import history, out
    assert history(db, deal)['total'] == 4
    assert out(deal)['source_created_at_kind'] == 'local'


def test_max_then_phone_share_the_same_card(db, intake):
    message(db)
    assert db.query(CRMEntity).filter_by(kind='deal').count() == 0
    chat_links.create_link(db, chat_links.LinkFromMax(max_chat_id=3,
        create=chat_links.CreateCard(kind='deal', name='Клиент', phone='79001112233')), intake[0])
    deal = db.query(CRMEntity).filter_by(kind='deal').one()
    call(db)
    assert db.query(CRMEntity).filter_by(kind='deal').count() == 1
    assert db.query(PhoneCall).one().deal_id == deal.id
    assert db.query(CRMChatLink).one().entity_id == deal.id


def test_hidden_max_phone_uses_stable_peer_and_never_name_matching(db, intake):
    db.add(CRMEntity(kind='contact', name='Клиент', payload={'phone': '79002223344'})); db.commit()
    message(db, number='')
    assert db.query(CRMEntity).filter_by(kind='deal').count() == 0
    chat_links.create_link(db, chat_links.LinkFromMax(max_chat_id=3,
        create=chat_links.CreateCard(kind='deal', name='Клиент')), intake[0])
    message(db, number='', message_id='m2')
    assert db.query(CRMEntity).filter_by(kind='deal').count() == 1
    assert db.query(CRMEntity).filter_by(kind='contact').count() == 2
    assert db.query(CRMEntity).filter_by(kind='deal').one().payload['is_unsorted'] is False


def test_unlinked_messages_create_one_notification_per_recipient_and_no_cards(db, intake):
    message(db)
    message(db)
    message(db, message_id='m2')
    db.expire_all()
    message(db, message_id='m3')
    assert db.query(CRMEntity).filter_by(kind='deal').count() == 0
    assert db.query(CRMEntity).filter_by(kind='contact').count() == 0
    assert db.query(MAXInboundMessage).count() == 3
    notifications = db.query(Notification).all()
    assert len(notifications) == 3
    assert all(n.object_type == 'max_unlinked_chat' and n.object_id == 3 for n in notifications)


def test_manager_global_write_denied_but_linked_card_send_allowed(db, intake, api, monkeypatch):
    from app.max import service as max_service
    calls = []
    monkeypatch.setattr(max_service, 'send_message', lambda *args, **kw: calls.append(args) or {'message': {'id': '1'}})
    manager = api(intake[0])
    for method, path, body in [
        ('post','/api/max/messages',{'chat_id':3,'text':'Test'}),
        ('post','/api/max/messages/forward',{'from_chat_id':3,'to_chat_id':4,'message_id':'1'}),
        ('patch','/api/max/messages',{'chat_id':3,'message_id':'1','text':'Test'}),
        ('post','/api/max/contacts',{'phone':'79001112233','first_name':'Test'}),
    ]:
        assert getattr(manager, method)(path, json=body).status_code == 403
    assert manager.post('/api/max/messages/attachment', data={'chat_id':'3','text':'Test'}).status_code == 403
    call(db)
    deal = db.query(CRMEntity).filter_by(kind='deal').one()
    path = f'/api/crm/entries/{deal.id}/chats/3/messages'
    assert manager.post(path, json={'text':'Test'}).status_code == 404
    chat_links.create_link(db, chat_links.LinkFromMax(max_chat_id=3, entity_id=deal.id), intake[0])
    assert manager.post(path, json={'text':'Test'}).status_code == 201
    assert calls == [(3, 'Test')]


def test_first_message_resolves_only_phones_from_card_and_never_sends(db, intake, api, monkeypatch):
    from app.max import service as max_service
    calls = []
    monkeypatch.setattr(max_service, 'start_dialog', lambda phone, name: calls.append((phone, name)) or {'chatId':3,'name':'Клиент'})
    monkeypatch.setattr(max_service, 'send_message', lambda *args, **kw: pytest.fail('Lookup must never send'))
    call(db)
    deal = db.query(CRMEntity).filter_by(kind='deal').one()
    client = api(intake[0])
    path = f'/api/crm/entries/{deal.id}'
    assert client.get(path + '/chat-phones').json()['items'][0]['phone'] == '+79001112233'
    assert client.post(path + '/chats/start', json={'phone':'79003334455'}).status_code == 422
    assert client.post(path + '/chats/start', json={'phone':'89001112233'}).status_code == 200
    assert len(calls) == 1 and db.query(CRMChatLink).one().entity_id == deal.id


def test_old_messages_staff_and_ambiguous_numbers_do_not_create_leads(db, intake):
    first, _, _ = intake
    message(db, time=1)
    assert db.query(MAXInboundMessage).count() == 0
    first.work_phone = '+79001112233'; db.commit()
    message(db)
    assert db.query(CRMEntity).filter_by(kind='deal').count() == 0
    first.work_phone = None
    db.add_all([CRMEntity(kind='contact', name=str(i), payload={'phone': '79001112233'}) for i in range(2)])
    db.commit()
    assert message(db).state == 'needs_link'
    call(db)
    assert db.query(PhoneCall).one().match_status == 'ambiguous'
    assert db.query(CRMEntity).filter_by(kind='deal').count() == 0


def test_outgoing_groups_and_unverified_sender_are_filtered_before_database():
    class Provider:
        def viewer_id(self): return '1'
        def chats(self): return [{'id': 3, 'type': 'CHAT'}]
    assert max_intake.prepare(Provider(), 3, {'sender': 1}) is None
    assert max_intake.prepare(Provider(), 3, {'sender': 3}) is None
    assert max_intake.prepare(Provider(), 3, {'sender': 2}) is None


def test_archived_lead_and_historical_replay_never_create_duplicates(db, intake):
    call(db)
    deal = db.query(CRMEntity).filter_by(kind='deal').one()
    deal.archived = True; db.commit()
    call(db, callid='second')
    service.ingest(db, {'cmd':'history','type':'in','phone':'79003334455','diversion':'79066773555',
        'start':'20261109T093000Z','callid':'archive','status':'Success'}, remember=False)
    assert db.query(CRMEntity).filter_by(kind='deal').count() == 1


@pytest.mark.parametrize('worked', [False, True])
def test_explicit_move_preserves_history_owner_and_worked_source(db, intake, worked):
    message(db)
    first, second, stage = intake
    source_id = chat_links.create_link(db, chat_links.LinkFromMax(max_chat_id=3,
        create=chat_links.CreateCard(kind='deal', name='Новый клиент', phone='79001112233')), first)['entity_id']
    message(db, message_id='m2')
    target = CRMEntity(kind='deal', name='Существующая заявка', payload={
        'pipeline_id': stage.payload['pipeline_id'], 'stage_id': stage.id, 'responsible_user_id': second.id})
    db.add(target)
    if worked:
        db.add(CRMEvent(entity_id=source_id, actor_id=first.id, action='max_message_sent',
                        details={'chat_id': 3, 'message_id': 'reply', 'text': 'MAX: Ответ менеджера'}))
    db.commit()
    result = chat_links.create_link(db, chat_links.LinkFromMax(max_chat_id=3, entity_id=target.id,
        move_from_id=source_id, move_from_version=1), first)
    db.expire_all()
    assert result['source_archived'] is (not worked)
    assert db.get(CRMEntity, source_id).archived is (not worked)
    assert db.get(CRMEntity, target.id).payload['responsible_user_id'] == second.id
    assert db.query(CRMChatLink).one().entity_id == target.id
    assert {r.entity_id for r in db.query(MAXInboundMessage)} == {target.id}
    assert db.query(CRMEvent).filter_by(entity_id=target.id, action='max_message_received').count() == 2
    if worked:
        assert db.query(CRMEvent).filter_by(entity_id=target.id, action='max_message_sent').count() == 1
    else:
        task = db.query(CRMEntity).filter_by(kind='task').one()
        assert task.payload['entity_id'] == target.id and task.payload['responsible_user_id'] == second.id
    assert db.query(Notification).filter_by(read_at=None).count() == 0
    message(db, message_id='m2')
    assert db.query(CRMEvent).filter_by(action='max_message_received').count() == 2


def test_stale_move_keeps_original_link_and_history(db, intake):
    from fastapi import HTTPException
    message(db)
    source_id = chat_links.create_link(db, chat_links.LinkFromMax(max_chat_id=3,
        create=chat_links.CreateCard(kind='deal', name='Клиент')), intake[0])['entity_id']
    target = CRMEntity(kind='contact', name='Существующий клиент', payload={})
    db.add(target); db.commit()
    with pytest.raises(HTTPException) as error:
        chat_links.create_link(db, chat_links.LinkFromMax(max_chat_id=3, entity_id=target.id,
            move_from_id=source_id, move_from_version=999), intake[0])
    assert error.value.status_code == 409
    assert db.query(CRMChatLink).one().entity_id == source_id
    assert db.query(MAXInboundMessage).one().entity_id == source_id


def test_card_attachment_permissions_and_single_send(db, intake, api, monkeypatch):
    from app.max import service as max_service
    from app.common.module_access import AccessLevel
    calls = []
    monkeypatch.setattr(max_service, 'send_message', lambda *args, **kw: calls.append((args, kw)) or {'message': {'id': 'file1'}})
    message(db)
    target = chat_links.create_link(db, chat_links.LinkFromMax(max_chat_id=3,
        create=chat_links.CreateCard(kind='deal', name='Клиент')), intake[0])['entity_id']
    client = api(intake[0])
    path = f'/api/crm/entries/{target}/chats/3/messages/attachment'
    upload = {'file': ('quote.txt', b'Quote', 'text/plain')}
    assert client.post(path.replace('/3/', '/4/'), files=upload).status_code == 404
    assert client.post(path, files={'file': ('empty.txt', b'', 'text/plain')}).status_code == 422
    assert calls == []
    assert client.post(path, files=upload, data={'text': 'Расчёт'}).status_code == 201
    assert calls == [((3, 'Расчёт'), {'file': (b'Quote', 'quote.txt', 'text/plain')})]
    event = db.query(CRMEvent).filter_by(action='max_message_sent').one()
    assert event.entity_id == target and event.actor_id == intake[0].id
