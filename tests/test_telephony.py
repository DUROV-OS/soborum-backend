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
        return httpx.Response(200,json={'message':{'content': '{"summary":"Обсуждали расчёт.","key_points":[{"text":"Есть расчёт","quote":"Пришлите расчёт"},{"text":"Оплачено","quote":"Оплатили миллион"}],"tasks":[]}'}})
    original=httpx.Client
    monkeypatch.setattr(worker.httpx,'Client',lambda **kw:original(transport=httpx.MockTransport(response)))
    result=worker.analyze('Пришлите расчёт завтра.')
    assert len(result['key_points'])==1
    assert result['key_points'][0]['quote']=='Пришлите расчёт'
