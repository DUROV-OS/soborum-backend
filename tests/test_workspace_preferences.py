from app.common.module_access import Module


def test_workspace_is_private_persistent_and_does_not_grant_access(api, make_user, db):
    admin = make_user(admin=True)
    manager = make_user(Module.CLIENTS)
    other = make_user(Module.ACCOUNTING)
    admin_client = api(admin)
    assert admin_client.patch(f'/api/auth/users/{manager.id}', json={'job_role': 'manager'}).status_code == 200
    client = api(manager)
    assert client.get('/api/auth/me/workspace').json() is None
    prefs = {'view': 'manager', 'blocks': ['personal_tasks', 'my_clients'], 'sections': ['clients']}
    assert client.put('/api/auth/me/workspace', json=prefs).json() == prefs
    db.expire_all()
    assert api(manager).get('/api/auth/me/workspace').json() == prefs
    assert api(other).get('/api/auth/me/workspace').json() is None
    assert api(manager).put('/api/auth/me/workspace', json={**prefs, 'view': 'director'}).status_code == 403
    assert api(manager).patch(f'/api/auth/users/{manager.id}', json={'job_role': 'director'}).status_code == 403
    assert api(manager).get('/api/accounting/money-movements').status_code == 403
    assert api(manager).put('/api/auth/me/workspace', json={**prefs, 'blocks': ['not-a-block']}).status_code == 422
    assert api(admin).patch(f'/api/auth/users/{manager.id}', json={'job_role': 'accountant'}).status_code == 200
    assert api(manager).get('/api/auth/me/workspace').json() is None
    assert api(manager).get('/api/auth/me').json()['job_role'] == 'accountant'
