from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy.orm import sessionmaker

from app.ai import meeting_worker
from app.ai.meeting_plan import Action, enqueue
from app.ai.models import Meeting, MeetingProcessing, MeetingTranscriptLine, MeetingStatus
from app.collaboration.calendar import Event
from app.common.module_access import Module, AccessLevel
from app.tasks.models import Task


def future_hour():
    from zoneinfo import ZoneInfo
    at = datetime.now(ZoneInfo('Europe/Moscow')).replace(hour=10, minute=0, second=0, microsecond=0)+timedelta(days=1)
    while at.weekday() > 4: at += timedelta(days=1)
    return at


def ready(db, owner, assignee, reviewer, *, scheduled=False):
    meeting = Meeting(owner_id=owner.id, status=MeetingStatus.FINISHED)
    db.add(meeting); db.flush()
    line = MeetingTranscriptLine(meeting_id=meeting.id, speaker='Участник', text='Подготовить расчёт до пятницы', at_ms=1000)
    db.add(line); db.flush()
    action = Action(id=uuid4(), title='Подготовить расчёт', expected_result='Расчёт в файле',
        assignee_id=assignee.id, reviewer_id=reviewer.id, deadline=future_hour()+timedelta(days=3),
        duration_minutes=60, scheduled_start=future_hour() if scheduled else None,
        source_line_ids=[line.id], quote=line.text).model_dump(mode='json')
    job = MeetingProcessing(meeting_id=meeting.id, status='ready', data={'actions': [action], 'transcript_ready': True, 'transcript_line_ids': [line.id]})
    db.add(job); db.commit()
    return meeting, job, action


def test_approval_native_tasks_calendar_idempotent_and_independent_deadline(db, api, make_user):
    owner = make_user(admin=True)
    assignee, reviewer = make_user(Module.TASKS), make_user(Module.TASKS)
    meeting, job, action = ready(db, owner, assignee, reviewer, scheduled=True)
    c = api(owner)
    body = {'version': job.version, 'request_id': str(uuid4()), 'action_ids': [action['id']]}
    r = c.post(f'/api/ai/meetings/{meeting.id}/plan/approve', json=body)
    assert r.status_code == 200, r.text
    task, event = db.query(Task).one(), db.query(Event).one()
    assert task.assignees == [assignee] and task.reviewers == [reviewer]
    assert task.link_meta['meeting_id'] == meeting.id
    assert task.link_meta['calendar_event_id'] == event.id
    assert task.deadline.replace(tzinfo=None) == datetime.fromisoformat(action['deadline']).replace(tzinfo=None)
    assert event.data['end'] != action['deadline']
    assert c.post(f'/api/ai/meetings/{meeting.id}/plan/approve', json=body).status_code == 200
    assert db.query(Task).count() == db.query(Event).count() == 1
    # A second client request cannot create the already approved task again.
    body.update(version=r.json()['version'], request_id=str(uuid4()))
    assert c.post(f'/api/ai/meetings/{meeting.id}/plan/approve', json=body).status_code == 200
    assert db.query(Task).count() == 1
    # Moving a time block keeps the actual task due date.
    update = {k: v for k, v in event.data.items() if k not in ('task_id', 'responses', 'attachments')}
    update.update(request_id=str(uuid4()), version=event.version)
    rr = c.patch(f'/api/projects/calendar/events/{event.id}', json=update)
    assert rr.status_code == 200, rr.text
    assert task.deadline.replace(tzinfo=None) == datetime.fromisoformat(action['deadline']).replace(tzinfo=None)
    # Removing the reserved work interval does not remove the employee's task.
    deleted = c.request('DELETE', f'/api/projects/calendar/events/{event.id}', json={'version': rr.json()['version'], 'request_id': str(uuid4())})
    assert deleted.status_code == 200, deleted.text
    assert not task.link_meta.get('calendar_event_id') and task.link_meta['meeting_id'] == meeting.id
    # The assigned task is available in the employee's existing task board.
    r = api(assignee).get('/api/tasks?scope=mine')
    assert r.status_code == 200 and any(t['id'] == task.id for t in r.json())


def test_stale_plan_source_tampering_and_permissions(db, api, make_user):
    owner, worker = make_user(admin=True), make_user(Module.AI)
    assignee, reviewer = make_user(Module.TASKS), make_user(Module.TASKS)
    meeting, job, action = ready(db, owner, assignee, reviewer)
    assert api(worker).put(f'/api/ai/meetings/{meeting.id}/plan', json={'version': 1, 'actions': [action]}).status_code == 404
    c = api(owner)
    assert c.put(f'/api/ai/meetings/{meeting.id}/plan', json={'version': 99, 'actions': [action]}).status_code == 409
    assert c.put(f'/api/ai/meetings/{meeting.id}/plan', json={'version': 1, 'actions': [{**action, 'quote': 'подмена'}]}).status_code == 422
    r = c.put(f'/api/ai/meetings/{meeting.id}/plan', json={'version': 1, 'actions': [{**action, 'title': 'Уточнённый расчёт'}]})
    assert r.status_code == 200 and db.query(Task).count() == 0
    meeting.owner_id = worker.id; db.commit()
    assert api(worker).post(f'/api/ai/meetings/{meeting.id}/plan/approve', json={'version': r.json()['version'], 'request_id': str(uuid4()), 'action_ids': [action['id']]}).status_code == 403


def test_private_busy_blocks_and_batch_rollback(db, api, make_user):
    from app.collaboration.calendar import Input
    owner, assignee, reviewer = make_user(admin=True), make_user(Module.TASKS), make_user(Module.TASKS)
    meeting, job, action = ready(db, owner, assignee, reviewer, scheduled=True)
    data = Input(request_id=uuid4(), title='Личное', start=future_hour(), end=future_hour()+timedelta(hours=1), category='personal').model_dump(mode='json', exclude={'request_id', 'version'})
    db.add(Event(owner_id=assignee.id, data=data)); db.commit()
    c = api(owner)
    r = c.post(f'/api/ai/meetings/{meeting.id}/plan/approve', json={'version': 1, 'request_id': str(uuid4()), 'action_ids': [action['id']]})
    assert r.status_code == 409 and db.query(Task).count() == 0
    # The organizer gets availability, not the employee's private event title.
    s = c.post(f'/api/ai/meetings/{meeting.id}/plan/slots', json={'action_id': action['id'], 'start': future_hour().isoformat(), 'end': (future_hour()+timedelta(days=1)).isoformat()})
    assert s.status_code == 200 and 'Личное' not in s.text
    assert all(datetime.fromisoformat(slot['start']) >= future_hour()+timedelta(hours=1) for slot in s.json()['slots'])
    # Two selected draft blocks cannot overlap each other either; no partial tasks survive.
    db.query(Event).delete()
    other = {**action, 'id': str(uuid4())}
    job.data = {**job.data, 'actions': [action, other]}; db.commit()
    r = c.post(f'/api/ai/meetings/{meeting.id}/plan/approve', json={'version': 1, 'request_id': str(uuid4()), 'action_ids': [action['id'], other['id']]})
    assert r.status_code == 409 and db.query(Task).count() == 0 and db.query(Event).count() == 0


def test_durable_worker_preserves_browser_lines_and_resumes_analysis(db, api, make_user):
    owner = make_user(admin=True); c = api(owner)
    mid = c.post('/api/ai/meetings', json={}).json()['id']
    c.post(f'/api/ai/meetings/{mid}/transcript', json={'lines': [{'text': 'черновой текст браузера', 'at_ms': 0}]})
    c.post(f'/api/ai/meetings/{mid}/audio', files={'file': ('meeting.webm', b'fake', 'audio/webm')})
    assert c.post(f'/api/ai/meetings/{mid}/finish').status_code == 200
    factory = sessionmaker(db.get_bind(), expire_on_commit=False)
    with factory() as s: id, token = meeting_worker.claim(s)
    calls = []
    def transcriber(path):
        calls.append(path)
        return [{'text': 'Подготовить расчёт', 'start': 4.2, 'end': 8}]
    def failed(*args): raise RuntimeError('private provider error')
    meeting_worker.process(id, token, transcriber, session=factory, analyzer=failed)
    db.expire_all()
    detail = c.get(f'/api/ai/meetings/{mid}').json()
    assert detail['processing']['status'] == 'error'
    assert detail['has_audio'] and detail['transcript'][0]['at_ms'] == 4200
    assert db.query(MeetingTranscriptLine).filter_by(meeting_id=mid).count() == 2
    assert 'private provider error' not in str(detail)
    assert c.post(f'/api/ai/meetings/{mid}/process').status_code == 200
    with factory() as s: id, new_token = meeting_worker.claim(s)
    analyzed = lambda *args: dict(summary='Резюме', decisions=[], questions=[], recommendations=['Проверить исходные данные'], actions=[])
    meeting_worker.process(id, token, transcriber, session=factory, analyzer=analyzed)  # fenced old worker
    meeting_worker.process(id, new_token, transcriber, session=factory, analyzer=analyzed)
    db.expire_all()
    detail = c.get(f'/api/ai/meetings/{mid}').json()
    assert len(calls) == 1 and detail['processing']['status'] == 'ready'
    assert detail['notes']['summary'] == 'Резюме' and detail['processing']['recommendations']
    assert db.query(Task).count() == 0


def test_silence_is_error_and_no_invented_transcript(db, api, make_user):
    owner = make_user(admin=True); c = api(owner)
    mid = c.post('/api/ai/meetings', json={}).json()['id']
    c.post(f'/api/ai/meetings/{mid}/audio', files={'file': ('silence.webm', b'fake', 'audio/webm')})
    c.post(f'/api/ai/meetings/{mid}/finish')
    factory = sessionmaker(db.get_bind(), expire_on_commit=False)
    with factory() as s: id, token = meeting_worker.claim(s)
    meeting_worker.process(id, token, lambda _: [], session=factory)
    db.expire_all()
    detail = c.get(f'/api/ai/meetings/{mid}').json()
    assert detail['processing']['status'] == 'error' and detail['has_audio']
    assert detail['transcript'] == [] and detail['notes'] is None


def test_speech_to_planner_handoff_is_durable(db, api, make_user):
    c = api(make_user(admin=True))
    mid = c.post('/api/ai/meetings', json={}).json()['id']
    c.post(f'/api/ai/meetings/{mid}/audio', files={'file': ('rec.webm', b'fake', 'audio/webm')})
    c.post(f'/api/ai/meetings/{mid}/finish')
    factory = sessionmaker(db.get_bind(), expire_on_commit=False)
    with factory() as s:
        assert meeting_worker.claim(s, 'analysis') is None
        id, token = meeting_worker.claim(s, 'speech')
    meeting_worker.process(id, token, lambda _: [{'text': 'Поручение', 'start': 1}], session=factory, speech_only=True)
    with factory() as s:
        assert meeting_worker.claim(s, 'speech') is None
        id, token = meeting_worker.claim(s, 'analysis')
    def must_not_transcribe(_): raise AssertionError('Audio must not be transcribed twice')
    meeting_worker.process(id, token, must_not_transcribe, session=factory,
        analyzer=lambda *args: dict(summary='Итог', decisions=[], questions=[], recommendations=[], actions=[]))
    db.expire_all()
    assert c.get(f'/api/ai/meetings/{mid}').json()['processing']['status'] == 'ready'


def test_model_requires_real_source_quotes(db, make_user, monkeypatch):
    from app.core import llm
    from app.core.config import settings
    meeting = SimpleNamespace(started_at=future_hour(), topic=None, goals=None, occurred_at=None, location=None, participants=None)
    lines = [SimpleNamespace(id=17, at_ms=1000, speaker='Участник', text='Надо подготовить расчёт')]
    value = dict(summary='Резюме', decisions=[], questions=[], recommendations=['Проверить расчёт'], actions=[
        dict(title='Выдуманное', quote='Несуществующая фраза', source_line_ids=[17]),
        dict(title='Расчёт', quote='Надо подготовить расчёт', source_line_ids=[17], assignee_id=None,
             expected_result='', deadline='не дата', duration_minutes=None, clarification='Уточнить исполнителя')])
    monkeypatch.setattr(llm, 'llm_client', lambda **kw: SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: SimpleNamespace(content=[SimpleNamespace(type='tool_use', name='submit_meeting_plan', input=value)]))))
    r = meeting_worker.generate(meeting, lines, [])
    assert len(r['actions']) == 1 and r['actions'][0]['deadline'] is None
    assert r['actions'][0]['source_line_ids'] == [17] and r['questions']


def test_speech_image_core_queue_handoff_and_stale_worker(db, api, make_user):
    from app.ai.meeting_speech_worker import SpeechQueue
    c = api(make_user(admin=True))
    mid = c.post('/api/ai/meetings', json={}).json()['id']
    c.post(f'/api/ai/meetings/{mid}/audio', files={'file': ('rec.webm', b'fake', 'audio/webm')})
    c.post(f'/api/ai/meetings/{mid}/finish')
    queue = SpeechQueue(db.get_bind())
    id, token = queue.claim()
    queue.process(id, 'old-token', lambda _: [{'start': 1, 'text': 'Не сохранять'}])
    queue.process(id, token, lambda _: [{'start': 1.2, 'text': 'Поручение'}])
    db.expire_all()
    detail = c.get(f'/api/ai/meetings/{mid}').json()
    assert detail['processing']['status'] == 'analyzing' and detail['transcript'][0]['text'] == 'Поручение'
    assert detail['transcript'][0]['at_ms'] == 1200 and queue.claim() is None
    factory = sessionmaker(db.get_bind(), expire_on_commit=False)
    with factory() as s: id, token = meeting_worker.claim(s, 'analysis')
    meeting_worker.process(id, token, lambda _: [], session=factory,
        analyzer=lambda *args: dict(summary='Готово', decisions=[], questions=[], recommendations=[], actions=[]))
    db.expire_all()
    assert c.get(f'/api/ai/meetings/{mid}').json()['processing']['status'] == 'ready'
