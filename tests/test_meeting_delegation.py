from datetime import datetime, timedelta, timezone
from uuid import uuid4
from app.ai.models import Meeting, MeetingProcessing
from app.common.module_access import Module, AccessLevel
from app.core.config import settings


def test_only_explicit_delegate_can_list_read_and_manage_other_owners_meetings(api, make_user, monkeypatch, db):
    owner, delegate, other = [make_user(admin=True) for _ in range(3)]
    mid = api(owner).post('/api/ai/meetings', json={'title': 'Доступное совещание'}).json()['id']
    private = api(other).post('/api/ai/meetings', json={'title': 'Частное совещание'}).json()['id']
    assert api(delegate).get(f'/api/ai/meetings/{mid}').status_code == 404
    assert api(delegate).get('/api/ai/meetings').json() == []
    monkeypatch.setattr(settings, 'meeting_delegations', {owner.id: [delegate.id]})
    assert [m['id'] for m in api(delegate).get('/api/ai/meetings').json()] == [mid]
    assert api(delegate).get(f'/api/ai/meetings/{mid}').status_code == 200
    assert api(delegate).get(f'/api/ai/meetings/{private}').status_code == 404
    assert api(other).get(f'/api/ai/meetings/{mid}').status_code == 404
    assert api(delegate).patch(f'/api/ai/meetings/{mid}', json={'title': 'Общий протокол'}).status_code == 200
    later = api(owner).post('/api/ai/meetings', json={}).json()['id']
    assert {m['id'] for m in api(delegate).get('/api/ai/meetings').json()} == {mid, later}
    assert db.get(Meeting, mid).owner_id == owner.id
    monkeypatch.setattr(settings, 'meeting_delegations', {})
    assert api(delegate).get(f'/api/ai/meetings/{mid}').status_code == 404
    assert api(owner).get(f'/api/ai/meetings/{mid}').status_code == 200


def test_delegated_record_audio_transcript_notes_protocol_plan_and_delete(api, make_user, monkeypatch, db):
    from app.ai import meeting_ask
    from app.ai.meeting_plan import Action
    from app.tasks.models import Task
    owner, delegate, reviewer = [make_user(admin=True) for _ in range(3)]
    monkeypatch.setattr(settings, 'meeting_delegations', {owner.id: [delegate.id]})
    mid = api(owner).post('/api/ai/meetings', json={}).json()['id']
    c = api(delegate)
    lines = c.post(f'/api/ai/meetings/{mid}/transcript', json={'lines': [{'text': 'Подготовить расчёт', 'at_ms': 1000}]}).json()
    assert c.patch(f'/api/ai/meetings/{mid}/transcript/{lines[0]["id"]}', json={'speaker': 'Участник'}).status_code == 200
    assert c.post(f'/api/ai/meetings/{mid}/audio', files={'file': ('meeting.webm', b'synthetic', 'audio/webm')}).status_code == 200
    assert c.get(f'/api/ai/meetings/{mid}/audio').content == b'synthetic'
    assert c.post(f'/api/ai/meetings/{mid}/finish').status_code == 200
    assert c.post(f'/api/ai/meetings/{mid}/process').status_code == 200
    assert c.get(f'/api/ai/meetings/{mid}/document').status_code == 200
    monkeypatch.setattr(meeting_ask, 'answer_meeting_question', lambda *args: 'Ответ Марины')
    assert c.post(f'/api/ai/meetings/{mid}/ask', json={'question': 'Что решили?'}).status_code == 200
    action = Action(id=uuid4(), title='Подготовить расчёт', expected_result='Файл', assignee_id=delegate.id,
        reviewer_id=reviewer.id, duration_minutes=60, deadline=datetime.now(timezone.utc)+timedelta(days=7),
        source_line_ids=[lines[0]['id']], quote='Подготовить расчёт').model_dump(mode='json')
    job = db.get(MeetingProcessing, mid)
    job.status = 'ready';job.data = {'actions': [action]};db.commit()
    r = c.put(f'/api/ai/meetings/{mid}/plan', json={'version': job.version, 'actions': [action]})
    assert r.status_code == 200, r.text
    now = datetime.now(timezone.utc)
    assert c.post(f'/api/ai/meetings/{mid}/plan/slots', json={'action_id': action['id'], 'start': now.isoformat(), 'end': (now+timedelta(days=7)).isoformat()}).status_code == 200
    r = c.post(f'/api/ai/meetings/{mid}/plan/approve', json={'version': r.json()['version'], 'request_id': str(uuid4()), 'action_ids': [action['id']]})
    assert r.status_code == 200, r.text
    assert db.query(Task).one().responsible_id == delegate.id
    assert db.get(Meeting, mid).owner_id == owner.id
    assert c.delete(f'/api/ai/meetings/{mid}').status_code == 204


def test_delegation_does_not_change_module_permissions(api, make_user, monkeypatch):
    owner = make_user(admin=True)
    viewer = make_user(Module.AI, level=AccessLevel.VIEW)
    mid = api(owner).post('/api/ai/meetings', json={}).json()['id']
    monkeypatch.setattr(settings, 'meeting_delegations', {owner.id: [viewer.id]})
    c = api(viewer)
    assert c.get(f'/api/ai/meetings/{mid}').status_code == 200
    assert c.patch(f'/api/ai/meetings/{mid}', json={'title': 'Запрещено'}).status_code == 403
    assert viewer.access_level(Module.TASKS) == AccessLevel.NONE
