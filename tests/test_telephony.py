from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
import pytest
from app.common.module_access import Module, AccessLevel
from app.core.config import settings
from app.crm.models import CRMEntity
from app.telephony.models import PhoneCall, PhoneCallAudit
from app.telephony import service, worker


def payload(**kw):
    return {'cmd':'history','callid':'call-1','phone':'8 (900) 111-22-33', 'type':'in',
            'user':'admin','start':'20261029T100000Z','duration':'60','status':'Success',
            'link':'https://records.example/one.mp3', **kw}


def test_webhook_auth_disabled_token_and_repeated_history(db, api, make_user, monkeypatch):
    client = api(make_user(admin=True))
    monkeypatch.setattr(settings,'megafon_crm_token','')
    assert client.post('/api/telephony/megafon',data=payload()).status_code == 503
    monkeypatch.setattr(settings,'megafon_crm_token','x'*40)
    assert client.post('/api/telephony/megafon',data=payload()).status_code == 401
    for _ in range(2):
        response=client.post('/api/telephony/megafon',data={**payload(),'crm_token':'x'*40})
        assert response.status_code == 200, response.text
    assert db.query(PhoneCall).count()==1
    row=db.query(PhoneCall).one()
    assert row.phone=='79001112233' and row.processing=='queued'
    assert 'crm_token' not in service.output(row)
    assert 'recording_url' not in service.output(row)
    assert client.post('/api/telephony/megafon', data={**payload(phone='79001112234'),'crm_token':'x'*40}).status_code==409


def test_exact_phone_match_ambiguous_and_manual_link_survives_retry(db, make_user, api):
    owner=make_user(admin=True)
    first=CRMEntity(kind='contact',name='Первый',payload={'phone':'+7 900 111 22 33'},archived=False)
    db.add(first);db.commit()
    service.ingest(db,payload())
    row=db.query(PhoneCall).one()
    assert row.crm_entity_id==first.id
    second=CRMEntity(kind='contact',name='Второй',payload={'phone':'89001112233'},archived=False)
    db.add(second);db.commit()
    service.ingest(db,payload())
    assert row.match_status=='ambiguous' and row.crm_entity_id is None
    client=api(owner)
    assert client.post(f'/api/crm/calls/{row.id}/link',json={'crm_entity_id':first.id}).status_code==200
    service.ingest(db,payload())
    assert row.match_status=='manual' and row.crm_entity_id==first.id
    assert db.query(PhoneCallAudit).count()==1
    assert service.listing(db,entity_id=first.id)['total']==1
    assert service.listing(db,entity_id=second.id)['total']==0


def test_recording_permissions_review_and_unavailable_audio(db,api,make_user,tmp_path,monkeypatch):
    service.ingest(db,payload())
    row=db.query(PhoneCall).one()
    denied=api(make_user(Module.WAREHOUSE))
    assert denied.get('/api/crm/calls').status_code==403
    assert denied.get(f'/api/crm/calls/{row.id}/audio').status_code==403
    reader=api(make_user(Module.CLIENTS,level=AccessLevel.VIEW))
    assert reader.get('/api/crm/calls').status_code==200
    assert reader.post(f'/api/crm/calls/{row.id}/review').status_code==403
    owner=api(make_user(admin=True))
    assert owner.post(f'/api/crm/calls/{row.id}/review').status_code==409
    monkeypatch.setattr(settings,'storage_dir',str(tmp_path))
    service.audio_path(row.id).parent.mkdir()
    service.audio_path(row.id).write_bytes(b'test-audio')
    row.audio_ready=True;db.commit()
    audio=owner.get(f'/api/crm/calls/{row.id}/audio')
    assert audio.status_code==200 and audio.content==b'test-audio'
    assert audio.headers['cache-control']=='private, no-store'
    assert owner.post(f'/api/crm/calls/{row.id}/review').json()['reviewed_at']


def test_calls_belong_to_one_application_and_manual_choice_survives_replay(db, api, make_user):
    contact = CRMEntity(kind='contact', name='Клиент', payload={'phone': '79001112233'})
    db.add(contact); db.flush()
    first = CRMEntity(kind='deal', name='Баня', payload={'contact_ids': [contact.id]})
    db.add(first); db.commit()
    service.ingest(db, payload())
    row = db.query(PhoneCall).one()
    assert row.deal_id == first.id
    assert service.listing(db, entity_id=first.id)['total'] == 1
    second = CRMEntity(kind='deal', name='Дом', payload={'contact_ids': [contact.id]})
    db.add(second); db.commit()
    service.ingest(db, payload())
    assert row.deal_id is None and row.deal_match_status == 'ambiguous'
    assert service.listing(db, entity_id=first.id)['total'] == 0
    assert service.listing(db, entity_id=second.id)['total'] == 0
    assert service.listing(db, entity_id=first.id, related=True)['total'] == 1
    client = api(make_user(admin=True))
    assert client.post(f'/api/crm/calls/{row.id}/link', json={'crm_entity_id': second.id}).status_code == 200
    service.ingest(db, payload())
    assert row.deal_id == second.id and row.deal_match_status == 'manual'
    assert row.crm_entity_id == contact.id
    assert service.listing(db, entity_id=first.id)['total'] == 0
    assert service.listing(db, entity_id=second.id)['total'] == 1
    assert service.listing(db, entity_id=contact.id)['total'] == 1
    assert service.listing(db, entity_id=second.id, related=True)['total'] == 0
    assert db.query(PhoneCallAudit).one().details['before']['deal_id'] is None


def test_inquiry_acceptance_preserves_call_and_chat_in_target_application(db, make_user):
    from app.crm import service as crm, communications
    from app.crm.chat_models import CRMChatLink
    from app.crm.schemas import Entry
    user = make_user(admin=True)
    pipeline = crm.create(db, 'pipeline', Entry(name='Продажи'), user)
    stage = crm.create(db, 'stage', Entry(name='Новая', data={'pipeline_id': pipeline['id']}), user)
    inquiry = crm.create(db, 'inquiry', Entry(name='Заявка', data={'phone':'79001112233'}), user)
    service.ingest(db, payload())
    call = db.query(PhoneCall).one()
    assert call.crm_entity_id == inquiry['id']
    communications.attach(db, inquiry['id'], 456, 'Клиент MAX', user)
    deal = crm.accept_inquiry(db, db.get(CRMEntity, inquiry['id']), Entry(name='Баня', data={'pipeline_id':pipeline['id'],'stage_id':stage['id']}), user)
    assert call.deal_id == deal['id'] and call.deal_match_status == 'inquiry'
    assert db.query(CRMChatLink).one().entity_id == deal['id']
    assert communications.links(db, deal['id'])['items'][0]['chat_id'] == 456
    assert communications.links(db, inquiry['id'])['items'][0]['chat_id'] == 456
    service.ingest(db, payload())
    assert call.deal_id == deal['id'] and call.deal_match_status == 'inquiry'
    assert service.listing(db, entity_id=deal['id'])['total'] == 1


def test_out_of_order_event_does_not_regress_completed_call(db):
    service.ingest(db,payload())
    service.ingest(db,payload(cmd='event',type='INCOMING',direction='in'))
    row=db.query(PhoneCall).one()
    assert row.status=='Success' and row.duration==60
    assert row.started_at is not None
    service.ingest(db,payload(link=''))
    assert row.recording_url=='https://records.example/one.mp3'


@pytest.mark.parametrize('url',[
    'http://records.example/a','https://localhost/a','https://records.example.evil/a',
    'https://user:secret@records.example/a','https://records.example:22/a',
])
def test_recording_url_rejects_unsafe_destinations(monkeypatch,url):
    monkeypatch.setattr(settings,'megafon_recording_hosts','records.example')
    with pytest.raises(ValueError): worker.recording_url(url)


def test_dns_private_address_is_denied(monkeypatch):
    monkeypatch.setattr(settings,'megafon_recording_hosts','records.example')
    monkeypatch.setattr(worker.socket,'getaddrinfo',lambda *a,**k:[(2,1,6,'',('127.0.0.1',443))])
    with pytest.raises(ValueError): worker.recording_url('https://records.example/a')


def test_worker_keeps_transcript_on_analysis_failure_and_retry(db,monkeypatch,tmp_path):
    monkeypatch.setattr(settings,'storage_dir',str(tmp_path))
    service.ingest(db,payload())
    id=worker.claim(db)
    @contextmanager
    def session(): yield db
    def download(id,url):
        path=service.audio_path(id);path.parent.mkdir(exist_ok=True);path.write_bytes(b'audio')
    monkeypatch.setattr(worker,'download',download)
    monkeypatch.setattr(worker,'transcribe',lambda *a:('Согласовали расчёт.',[{'start':0,'end':1,'text':'Согласовали расчёт.'}]))
    monkeypatch.setattr(worker,'analyze',lambda *a:(_ for _ in ()).throw(ValueError('secret-url')))
    worker.process(id,None,session)
    row=db.get(PhoneCall,id)
    assert row.audio_ready and row.transcript=='Согласовали расчёт.' and row.processing=='error'
    assert 'secret-url' not in row.error
    monkeypatch.setattr(worker,'analyze',lambda *a:{'summary':'Согласовали расчёт.'})
    worker.process(id,None,session)
    assert row.processing=='ready' and row.analysis['summary']=='Согласовали расчёт.'


def test_expired_worker_lease_can_be_recovered(db):
    service.ingest(db,payload())
    id=worker.claim(db)
    assert worker.claim(db) is None
    row=db.get(PhoneCall,id)
    row.lease_until=datetime.now(timezone.utc)-timedelta(seconds=1);db.commit()
    assert worker.claim(db)==id


def test_archive_replay_updates_existing_call_and_catches_late_recording(db,monkeypatch):
    from app.telephony import archive
    service.ingest(db,payload(link=''))
    monkeypatch.setattr(archive,'history',lambda *a:[{
        'uid':'call-1','client':'79001112233','type':'in','start':'2026-10-29T10:00:00Z',
        'duration':60,'status':'success','record':'https://records.example/late.mp3'}])
    monkeypatch.setattr(archive.time,'sleep',lambda *a:None)
    start=datetime(2026,10,29,tzinfo=timezone.utc)
    archive.sync(db,start,start+timedelta(days=1))
    archive.sync(db,start,start+timedelta(days=1))
    row=db.query(PhoneCall).one()
    assert row.recording_url.endswith('/late.mp3') and row.processing=='queued'


def test_analysis_keeps_only_points_with_real_quotes(monkeypatch):
    import httpx
    monkeypatch.setattr(settings,'call_analysis_url','http://127.0.0.1:11434')
    def response(request):
        return httpx.Response(200,json={'message':{'content': '{"summary":"Обсуждали расчёт.","key_points":[{"text":"Есть расчёт","quote":"Пришлите расчёт"},{"text":"Оплачено","quote":"Оплатили миллион"}],"tasks":[{"text":"Назначить встречу в пятницу","quote":"Пока не думаем об этом"},{"text":"Прислать через неделю","quote":"Пришлите расчёт"}]}'}})
    original=httpx.Client
    monkeypatch.setattr(worker.httpx,'Client',lambda **kw:original(transport=httpx.MockTransport(response)))
    result=worker.analyze('Пришлите расчёт завтра. Пока не думаем об этом')
    assert len(result['key_points'])==1
    assert result['key_points'][0]['quote']=='Пришлите расчёт'
    assert result['tasks']==[{'text':'Пришлите расчёт','quote':'Пришлите расчёт'}]


def test_analysis_never_displays_invented_paraphrases_with_genuine_quotes(monkeypatch):
    import json
    import httpx
    monkeypatch.setattr(settings, 'call_analysis_url', 'http://127.0.0.1:11434')
    text = 'Хочу заказать баню. Пришлите расчёт завтра.'
    invented = {'summary': 'Покупка отложена, договорились связаться через месяц.',
        'key_points': [{'text': 'Покупка отложена', 'quote': 'Пришлите расчёт завтра.'}] * 3,
        'tasks': [{'text': 'Позвонить через месяц', 'quote': text}] * 3}
    def response(*args, **kwargs):
        return httpx.Response(200, request=httpx.Request('POST', args[0]), json={'message': {'content': json.dumps(invented)}})
    @contextmanager
    def client(**kwargs):
        class Fake:
            post = staticmethod(response)
        yield Fake()
    monkeypatch.setattr(worker.httpx, 'Client', client)
    result = worker.analyze(text)
    assert 'отложена' not in result['summary'] and 'месяц' not in result['summary']
    assert 'Хочу заказать баню.' in result['summary']
    assert result['tasks'] == [{'text': 'Пришлите расчёт завтра.', 'quote': 'Пришлите расчёт завтра.'}]
    assert all(point['text'] == point['quote'] and point['quote'] in text for point in result['key_points'])
