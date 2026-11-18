from uuid import uuid4
from pathlib import Path
from app.common.module_access import Module, AccessLevel
from app.collaboration.models import Member, ProjectMessage as Message, Project, ProjectFile, ProjectTask
from app.core.config import settings
from app.tasks.models import Task


def make_project(api, owner, members=(), **extra):
    body = dict(name='Проект дома', owner_id=owner.id, member_ids=[u.id for u in members], request_id=str(uuid4()), **extra)
    result = api(owner).post('/api/projects', json=body)
    assert result.status_code == 200, result.text
    return result.json(), body


def test_private_project_membership_revocation_and_no_module_expansion(api, make_user, db):
    owner, member, outsider = make_user(admin=True), make_user(), make_user()
    row, body = make_project(api, owner, [member])
    id = row['id']
    assert api(outsider).get('/api/projects').json()['items'] == []
    assert api(outsider).get(f'/api/projects/{id}').status_code == 404
    assert api(member).get(f'/api/projects/{id}').status_code == 200
    message = dict(body='Внутреннее обсуждение', request_id=str(uuid4()))
    assert api(member).post(f'/api/projects/{id}/messages', json=message).status_code == 200
    assert api(member).get(f'/api/projects/{id}/tasks').json() == []
    change = {k: v for k, v in body.items() if k != 'request_id'} | {'version': row['version'], 'status': 'active', 'member_ids': []}
    assert api(member).patch(f'/api/projects/{id}', json=change).status_code == 403
    assert api(owner).patch(f'/api/projects/{id}', json=change).status_code == 200
    assert api(member).get(f'/api/projects/{id}/messages').status_code == 404
    assert api(member).post(f'/api/projects/{id}/messages', json=message).status_code == 404
    assert api(owner).patch(f'/api/projects/{id}', json=change).status_code == 409


def test_idempotency_messages_cross_project_replies_and_mentions(api, make_user, db):
    owner, member, outsider = make_user(admin=True), make_user(), make_user()
    row, body = make_project(api, owner, [member])
    assert api(owner).post('/api/projects', json=body).json()['id'] == row['id']
    assert api(owner).post('/api/projects', json=body | {'name': 'Другое'}).status_code == 409
    second, _ = make_project(api, owner)
    first = api(owner).post(f"/api/projects/{second['id']}/messages", json={'body': 'Другой проект', 'request_id': str(uuid4())}).json()
    payload = {'body': 'Вопрос участнику', 'mention_ids': [member.id], 'request_id': str(uuid4())}
    route = f"/api/projects/{row['id']}/messages"
    sent = api(owner).post(route, json=payload)
    assert sent.status_code == 200
    assert api(owner).post(route, json=payload).json()['id'] == sent.json()['id']
    assert api(owner).post(route, json=payload | {'body': 'Изменено'}).status_code == 409
    assert api(owner).post(route, json=payload | {'request_id': str(uuid4()), 'reply_to_id': first['id']}).status_code == 422
    assert api(owner).post(route, json=payload | {'request_id': str(uuid4()), 'mention_ids': [outsider.id]}).status_code == 422
    assert api(member).get('/api/projects').json()['items'][0]['unread'] == 1
    assert api(member).post(f"/api/projects/{row['id']}/read", json={'message_id': first['id']}).status_code == 422
    assert api(member).post(f"/api/projects/{row['id']}/read", json={'message_id': sent.json()['id']}).status_code == 200
    assert api(member).get('/api/projects').json()['items'][0]['unread'] == 0


def test_files_are_scoped_verified_and_retry_safe(api, make_user, tmp_path, monkeypatch, db):
    monkeypatch.setattr(settings, 'storage_dir', str(tmp_path))
    owner, member = make_user(admin=True), make_user()
    row, body = make_project(api, owner, [member]); other, _ = make_project(api, owner)
    path = f"/api/projects/{row['id']}/files"; key = str(uuid4())
    uploaded = api(member).post(path, data={'request_id': key}, files={'file': ('notes.txt', b'proof', 'text/plain')})
    assert uploaded.status_code == 200, uploaded.text
    file = uploaded.json(); assert len(file['sha256']) == 64
    assert api(member).post(path, data={'request_id': key}, files={'file': ('notes.txt', b'proof', 'text/plain')}).json()['id'] == file['id']
    assert api(member).post(path, data={'request_id': key}, files={'file': ('notes.txt', b'changed', 'text/plain')}).status_code == 409
    assert api(member).post(path, data={'request_id': str(uuid4())}, files={'file': ('script.html', b'<script/>', 'text/html')}).status_code == 422
    assert api(owner).get(f"/api/projects/{other['id']}/files/{file['id']}").status_code == 404
    download = path + '/' + str(file['id'])
    assert api(member).get(download).content == b'proof'
    db.delete(db.get(Member, (row['id'], member.id))); db.commit()
    assert api(member).get(download).status_code == 404
    asset = db.get(ProjectFile, file['id']); Path(asset.path).write_bytes(b'false')
    assert api(owner).get(download).status_code == 409


def test_native_task_result_review_and_project_completion(api, make_user, db):
    owner, worker, viewer = make_user(admin=True), make_user(Module.TASKS), make_user(Module.TASKS, level=AccessLevel.VIEW)
    row, body = make_project(api, owner, [worker, viewer])
    route = f"/api/projects/{row['id']}/tasks"
    task_body = {'title': 'Проверить комплектность', 'assignee_id': worker.id, 'reviewer_id': owner.id, 'request_id': str(uuid4())}
    assert api(viewer).post(route, json=task_body).status_code == 403
    assert api(owner).post(route, json=task_body | {'reviewer_id': worker.id}).status_code == 422
    created = api(owner).post(route, json=task_body); assert created.status_code == 200, created.text
    task_id = created.json()['task_id']
    assert api(owner).post(route, json=task_body).json()['task_id'] == task_id
    assert len(api(worker).get(route).json()) == 1
    assert api(viewer).get(route).json() == []
    assert db.get(Task, task_id).description is None
    change = {k: v for k, v in body.items() if k != 'request_id'} | {'version': row['version'], 'status': 'completed'}
    assert api(owner).patch(f"/api/projects/{row['id']}", json=change).status_code == 409
    assert api(worker).patch(f'/api/tasks/{task_id}/status', json={'status': 'in_progress'}).status_code == 200
    report = api(worker).post(f'/api/tasks/{task_id}/report', data={'comment': 'Комплектность проверена'})
    assert report.status_code == 200, report.text
    assert report.json()['status'] == 'in_review'
    accepted = api(owner).post(f'/api/tasks/{task_id}/review', data={'accept': 'true', 'comment': 'Результат принят'})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()['status'] == 'done'
    assert api(owner).patch(f"/api/projects/{row['id']}", json=change).status_code == 200
    assert api(worker).post(f"/api/projects/{row['id']}/messages", json={'body': 'Поздно', 'request_id': str(uuid4())}).status_code == 409


def test_search_pagination_and_closed_project(api, make_user, db):
    owner = make_user(admin=True); row, body = make_project(api, owner)
    for i in range(105):
        db.add(Message(project_id=row['id'], author_id=owner.id, body=f'Сообщение {i}', request_id=str(uuid4()), request_hash='0'*64))
    db.commit()
    route = f"/api/projects/{row['id']}/messages"
    first = api(owner).get(route).json(); assert len(first['items']) == 100 and first['has_more']
    old = api(owner).get(route, params={'before': first['items'][0]['id']}).json()
    assert len(old['items']) == 5 and not old['has_more']
    assert api(owner).get(route, params={'query': 'Сообщение 104'}).json()['items'][0]['body'] == 'Сообщение 104'


def test_project_creation_permissions_and_link_validation(api, make_user):
    viewer = make_user(Module.TASKS, level=AccessLevel.VIEW)
    creator = make_user(Module.TASKS)
    body = {'name': 'New', 'owner_id': creator.id, 'request_id': str(uuid4())}
    assert api(viewer).post('/api/projects/', json=body).status_code == 403
    assert api(creator).post('/api/projects/', json=body | {'client_id': 1}).status_code == 403
    row, _ = make_project(api, creator)
    assert api(creator).get('/api/projects/', params={'status': 'all'}).json()['total'] == 1
    assert not api(creator).get('/api/projects/options').json()['can_link_clients']


def test_project_task_cannot_be_deleted_or_self_accepted(api, make_user, db):
    owner, worker = make_user(admin=True), make_user(Module.TASKS)
    row, _ = make_project(api, owner, [worker])
    response = api(owner).post(f"/api/projects/{row['id']}/tasks", json={
        'title': 'Accepted result required', 'assignee_id': worker.id, 'reviewer_id': owner.id, 'request_id': str(uuid4())})
    assert response.status_code == 200, response.text
    task_id = response.json()['task_id']
    assert api(owner).delete(f'/api/tasks/{task_id}').status_code == 409
    task = db.get(Task, task_id); task.reviewers = [worker]; task.responsible_id = None; db.commit()
    assert api(worker).patch(f'/api/tasks/{task_id}/status', json={'status':'in_progress'}).status_code == 200
    result = api(worker).post(f'/api/tasks/{task_id}/report', data={'comment':'Done'})
    assert result.status_code == 200 and result.json()['status'] == 'in_review'
    assert api(worker).post(f'/api/tasks/{task_id}/review', data={'accept':'true'}).status_code in (403,409)
