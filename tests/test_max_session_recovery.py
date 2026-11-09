import json
import os
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.core.config import settings
from app.max import authentication as auth, client, connection, listener, session_store as store


@pytest.fixture
def private_store(monkeypatch, tmp_path):
    path = tmp_path / 'private' / 'max.sqlite3'
    monkeypatch.setattr(settings, 'max_session_file', str(path))
    monkeypatch.setattr(settings, 'max_token', 'legacy-secret')
    monkeypatch.setattr(settings, 'storage_dir', str(tmp_path / 'public'))
    yield path


@pytest.fixture(autouse=True)
def no_pending_attempt(monkeypatch):
    monkeypatch.setattr(auth, '_attempt', None)
    listener._wake.clear()
    yield
    if auth._attempt:
        auth._discard(auth._attempt, 'cancelled')
    listener._wake.clear()


def test_session_survives_reopen_and_legacy_token_is_imported_only_once(private_store, monkeypatch):
    first = store.read()
    generation = store.activate(first['generation'], 'new-secret', 'stable-device', '101', 'Company', 1)
    monkeypatch.setattr(settings, 'max_token', 'obsolete-secret')
    again = store.read()
    assert again['generation'] == generation and again['token'] == 'new-secret'
    assert again['device_id'] == 'stable-device'
    public = json.dumps(store.public_status())
    assert all(secret not in public for secret in ('secret', 'stable-device', 'generation'))
    if os.name == 'posix':
        assert stat.S_IMODE(private_store.stat().st_mode) == 0o600
        assert stat.S_IMODE(private_store.parent.stat().st_mode) == 0o700


def test_credentials_cannot_be_stored_in_public_media(private_store, monkeypatch):
    monkeypatch.setattr(settings, 'max_session_file', str(private_store.parent.parent / 'public' / 'session.db'))
    with pytest.raises(store.StoreUnavailable):
        store.read()


def test_simultaneous_rotation_cannot_overwrite_new_credentials(private_store):
    snapshot = store.read()
    gate = threading.Barrier(2)
    def rotate(token):
        gate.wait()
        return store.verified(snapshot, token, '101', 'Company')
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(rotate, ['secret-a', 'secret-b']))
    assert sorted(x for x in results if x is not None) == [2]
    assert sum(x is None for x in results) == 1
    store.record(1, state='reauth_required')
    assert store.read()['state'] == 'connected'


def test_account_is_pinned_and_stale_confirmation_cannot_replace_it(private_store):
    old = store.read()
    store.activate(old['generation'], 'one', 'device', '101', 'Company', 1)
    with pytest.raises(ValueError):
        store.activate(old['generation'], 'two', 'device2', '101', 'Company', 1)
    with pytest.raises(ValueError):
        store.activate(2, 'two', 'device2', '202', 'Personal', 1)
    assert store.read()['token'] == 'one'


class FakeSession:
    opened = []
    failure = None
    new_token = 'rotated-secret'
    account = '101'
    def __init__(self, token, *, device_id=None):
        self.token, self.device_id, self.closed = token, device_id, False
        self.login = {'profile': {'id': self.account, 'name': 'Company'}}
    def open(self):
        self.opened.append(self)
        if self.failure:
            raise self.failure
        self.token = self.new_token
        return self
    def viewer_id(self):
        return self.account
    def close(self):
        self.closed = True


@pytest.fixture
def fake_session(monkeypatch):
    monkeypatch.setattr(FakeSession, 'opened', [])
    monkeypatch.setattr(FakeSession, 'failure', None)
    monkeypatch.setattr(FakeSession, 'account', '101')
    monkeypatch.setattr(client, 'MaxSession', FakeSession)
    monkeypatch.setattr(auth, 'MaxSession', FakeSession)
    return FakeSession


def test_rotation_uses_stable_device_and_is_saved_before_session_is_used(private_store, fake_session):
    device = store.read()['device_id']
    session = connection.open_session()
    assert session.device_id == device and store.read()['token'] == 'rotated-secret'
    assert session.managed_generation == 2


def test_definite_revocation_stops_repeated_network_logins(private_store, fake_session):
    fake_session.failure = client.AuthRejected()
    for _ in range(2):
        with pytest.raises(client.AuthRejected):
            connection.open_session()
    assert len(fake_session.opened) == 1
    assert store.public_status()['state'] == 'reauth_required'


def test_transient_failure_recovers_without_owner_qr(private_store, fake_session):
    fake_session.failure = ConnectionError('private provider detail')
    with pytest.raises(ConnectionError):
        connection.open_session()
    assert store.public_status()['state'] == 'reconnecting'
    fake_session.failure = None
    connection.open_session()
    assert store.public_status()['state'] == 'connected'


def test_concurrent_refresh_is_retried_once_with_latest_credentials(private_store, fake_session, monkeypatch):
    previous = fake_session.open
    def refreshed(self):
        if self.token == 'legacy-secret':
            fake_session.opened.append(self)
            snapshot = store.read()
            store.verified(snapshot, 'other-worker-token', '101', 'Company')
            raise client.AuthRejected()
        return previous(self)
    monkeypatch.setattr(fake_session, 'open', refreshed)
    connection.open_session()
    assert len(fake_session.opened) == 2
    assert store.read()['state'] == 'connected'


def test_send_failure_never_reopens_or_replays(private_store, fake_session):
    sends = []
    with pytest.raises(ConnectionError):
        with client.session() as session:
            sends.append('one explicit send')
            raise ConnectionError('unknown send outcome')
    assert sends == ['one explicit send'] and len(fake_session.opened) == 1
    assert session.closed


def pending(private_store, phase='confirm_account'):
    snapshot = store.read()
    job = auth.Attempt(str(uuid4()), 1, snapshot['generation'], phase=phase,
                       token='candidate-secret', account_id='101', account_name='Company')
    auth._attempt = job
    return job


def test_attempt_status_hides_credentials_and_is_bound_to_owner(private_store):
    job = pending(private_store)
    with pytest.raises(HTTPException) as denied:
        auth.get(job.id, 2)
    assert denied.value.status_code == 404
    public = json.dumps(auth.get(job.id, 1))
    assert 'candidate-secret' not in public and job.device_id not in public
    auth.cancel(job.id, 1)
    assert not job.token and not job.password and not job.qr_image


def test_expired_attempt_cannot_be_confirmed(private_store, fake_session):
    job = pending(private_store)
    job.expires_at = time.time() - 1
    with pytest.raises(HTTPException):
        auth.confirm(job.id, 1)
    assert job.phase == 'expired' and not job.token and not fake_session.opened


def test_expiry_during_confirmation_never_commits_credentials(private_store, fake_session, monkeypatch):
    job = pending(private_store)
    previous = fake_session.open
    def expire(self):
        result = previous(self)
        auth._expire(job)
        return result
    monkeypatch.setattr(fake_session, 'open', expire)
    with pytest.raises(HTTPException) as error:
        auth.confirm(job.id, 1)
    assert error.value.status_code == 409
    assert store.read()['token'] == 'legacy-secret'


def test_confirm_is_idempotent_and_wakes_listener(private_store, fake_session, monkeypatch):
    job = pending(private_store)
    starts = []
    monkeypatch.setattr(listener, 'start_max_listener', lambda: starts.append(1))
    assert auth.confirm(job.id, 1)['phase'] == 'complete'
    assert auth.confirm(job.id, 1)['phase'] == 'complete'
    assert len(fake_session.opened) == 1 and starts == [1] and listener._wake.is_set()
    assert store.read()['token'] == 'rotated-secret' and not job.token


def test_new_qr_cannot_cancel_inflight_commit(private_store):
    job = pending(private_store, 'connecting')
    with pytest.raises(HTTPException) as error:
        auth.start(uuid4(), 1)
    assert error.value.status_code == 409 and auth._attempt is job


def test_qr_accepts_provider_links_only():
    assert auth._qr_image('https://max.ru/qr/test').startswith('data:image/png;base64,')
    for link in ['http://max.ru/test', 'https://max.ru.evil.test/', 'https://evil.test/', 'https://user@max.ru/test']:
        with pytest.raises(RuntimeError):
            auth._qr_image(link)


def test_qr_exchange_requires_owner_confirmation_before_persist(private_store, fake_session, monkeypatch):
    job = pending(private_store, 'starting')
    job.token = ''
    monkeypatch.setattr(fake_session, 'open_transport', lambda self: self, raising=False)
    frames = iter([
        (288, {'expiresAt': (time.time() + 60) * 1000, 'pollingInterval': 1000,
               'trackId': 'private-track', 'qrLink': 'https://max.ru/qr/private'}),
        (289, {'status': {'loginAvailable': True}}),
        (291, {'tokenAttrs': {'LOGIN': {'token': 'qr-secret'}}}),
    ])
    def request(session, opcode, payload):
        expected, body = next(frames)
        assert opcode == expected
        return body
    monkeypatch.setattr(auth, '_request', request)
    auth._run(job)
    assert job.phase == 'confirm_account' and job.account_id == '101'
    assert store.read()['token'] == 'legacy-secret'


@pytest.mark.parametrize('method,path', [('get', ''), ('post', '/check'), ('post', '/qr'),
                                       ('get', '/qr/00000000-0000-0000-0000-000000000001'),
                                       ('post', '/qr/00000000-0000-0000-0000-000000000001/confirm')])
def test_managers_cannot_manage_corporate_credentials(private_store, api, make_user, method, path):
    result = getattr(api(make_user()), method)('/api/max/connection' + path,
                  **({'json': {'request_id': str(uuid4())}} if method == 'post' else {}))
    assert result.status_code == 403


def test_admin_status_is_not_cacheable_and_never_contains_token(private_store, api, make_user):
    response = api(make_user(admin=True)).get('/api/max/connection')
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    assert 'legacy-secret' not in response.text and 'device_id' not in response.text


def test_listener_survives_storage_error_while_reporting_network_failure(private_store, monkeypatch):
    fake = FakeSession('one')
    fake.managed_generation = 1
    monkeypatch.setattr(listener, '_open_session', lambda: fake)
    monkeypatch.setattr(listener, '_listen', lambda _: (_ for _ in ()).throw(ConnectionError()))
    monkeypatch.setattr(store, 'record', lambda *a, **k: (_ for _ in ()).throw(OSError('disk temporarily unavailable')))
    class Stop(BaseException): pass
    waits = []
    def wait(seconds):
        waits.append(seconds)
        raise Stop
    monkeypatch.setattr(listener._wake, 'wait', wait)
    with pytest.raises(Stop):
        listener._run()
    assert fake.closed and waits
