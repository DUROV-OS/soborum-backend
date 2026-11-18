import imaplib
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.core.config import settings
from app.core.security import create_access_token
from app.db.session import get_db
from app.mail import service
from app.mail.router import app as mail_app
from app.main import app


@pytest.fixture
def imap(monkeypatch):
    message = EmailMessage()
    message['Subject'] = 'Расчёт дома'
    message['From'] = 'Клиент <client@example.test>'
    message['To'] = 'sales@example.test'
    message.set_content('Нужен расчёт дома.')
    message.add_attachment(b'example-document', maintype='application', subtype='pdf', filename='План.pdf')

    class FakeIMAP:
        calls = []
        validity = b'81'
        fail = False
        size = len(message.as_bytes())
        seen = True

        def __init__(self, host, port, **kwargs):
            assert host == settings.shared_mail_host
            assert port == 993
            assert kwargs['ssl_context'].check_hostname
            self.calls.append(('connect',))

        def login(self, address, password):
            if self.fail:
                raise imaplib.IMAP4.error('private-test-password private-account')
            return 'OK', [b'logged in']

        def select(self, folder, readonly=False):
            self.calls.append(('select', folder, readonly))
            return 'OK', [b'3']

        def list(self):
            return 'OK', [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren) "/" "Sent"', b'(\\Noselect) "/" "Archive"']

        def status(self, folder, fields):
            return 'OK', [b'"INBOX" (MESSAGES 300 UNSEEN 42)']

        def response(self, code):
            return code, [self.validity]

        def uid(self, command, *args):
            self.calls.append((command, *args))
            if command == 'search':
                return 'OK', [b'2 7 9']
            if command == 'store':
                type(self).seen = args[1].startswith('+')
                return 'OK', []
            if args[1] == '(UID FLAGS)':
                flags = r'\Seen' if self.seen else ''
                return 'OK', [f'1 (UID {args[0]} FLAGS ({flags}))'.encode()]
            if args[1] == '(UID RFC822.SIZE)':
                return 'OK', [f'1 (UID {args[0]} RFC822.SIZE {self.size})'.encode()]
            if 'HEADER.FIELDS' in args[1]:
                return 'OK', [(f'1 (UID {uid} FLAGS (\\Seen) RFC822.SIZE {self.size})'.encode(), message.as_bytes()) for uid in args[0].split(',')]
            return 'OK', [(f'1 (UID {args[0]} BODY[] {{{self.size}}})'.encode(), message.as_bytes())]

        def logout(self):
            self.calls.append(('logout',))

    monkeypatch.setattr(settings, 'shared_mail_enabled', True)
    monkeypatch.setattr(settings, 'shared_mail_password', SecretStr('private-test-password'))
    monkeypatch.setattr(service.imaplib, 'IMAP4_SSL', FakeIMAP)
    return FakeIMAP


def test_admin_can_read_shared_mail(api, make_user, imap):
    client = api(make_user(admin=True))
    response = client.get('/api/mail/messages?limit=2')
    assert response.status_code == 200
    data = response.json()
    assert [item['uid'] for item in data['items']] == [9, 7]
    assert data['uidvalidity'] == '81'
    assert data['next_before'] == 7
    assert data['items'][0]['subject'] == 'Расчёт дома'
    assert data['items'][0]['unread'] is False
    assert response.headers['cache-control'] == 'no-store'
    older = client.get('/api/mail/messages?before=7').json()
    assert [item['uid'] for item in older['items']] == [2]
    assert older['next_before'] is None


def test_message_and_attachment_are_read_only(api, make_user, imap):
    client = api(make_user(admin=True))
    data = client.get('/api/mail/messages/9?uidvalidity=81').json()
    assert data['text'].strip() == 'Нужен расчёт дома.'
    attachment = data['attachments'][0]
    assert attachment['filename'] == 'План.pdf'
    response = client.get(f"/api/mail/messages/9/attachments/{attachment['part']}?uidvalidity=81")
    assert response.content == b'example-document'
    assert response.headers['content-type'] == 'application/octet-stream'
    assert response.headers['content-disposition'].startswith('attachment;')
    assert response.headers['x-content-type-options'] == 'nosniff'
    assert all(call[0] in {'connect', 'select', 'fetch', 'logout'} for call in imap.calls)
    assert all('BODY.PEEK' in call[2] or call[2] == '(UID RFC822.SIZE)' for call in imap.calls if call[0] == 'fetch')
    assert all(call[2] is True for call in imap.calls if call[0] == 'select')


def test_explicit_shared_mail_access_and_denial(api,make_user,imap,monkeypatch):
    user=make_user()
    client=api(user)
    assert client.get('/api/mail/status').json()['can_access'] is False
    for path in ('/folders','/messages','/messages/9?uidvalidity=81','/messages/9/attachments/2?uidvalidity=81'):
        assert client.get('/api/mail'+path).status_code==403
    assert not imap.calls
    monkeypatch.setattr(settings,'shared_mail_allowed_user_ids',[user.id])
    assert client.get('/api/mail/status').json()['can_access'] is True
    assert client.get('/api/mail/messages').status_code==200


def test_folder_counts_search_scope_and_flags(api,make_user,imap):
    client=api(make_user(admin=True))
    rows=client.get('/api/mail/folders').json()
    assert [f['name'] for f in rows]==['Входящие','Sent']
    assert rows[0]['unread']==42 and rows[0]['total']==300
    folder=rows[1]['id']
    result=client.get('/api/mail/messages',params={'folder_id':folder,'q':'дом "А"'})
    assert result.status_code==200,result.text
    assert ('select','"Sent"',True) in imap.calls
    searches=[c for c in imap.calls if c[0]=='search']
    assert searches[-1][1:3]==('CHARSET','UTF-8')
    assert searches[-1][-1]=='"дом \\"А\\""'.encode('utf-8')
    params={'folder_id':folder,'uidvalidity':'81'}
    result=client.patch('/api/mail/messages/9/flags',params=params,json={'read':False})
    assert result.status_code==200,result.text
    assert result.json()['unread'] is True
    assert ('select','"Sent"',False) in imap.calls
    assert client.patch('/api/mail/messages/9/flags',params={**params,'uidvalidity':'80'},json={'read':True}).status_code==409


def test_mail_folder_and_query_validation(api,make_user,imap):
    client=api(make_user(admin=True))
    for params in ({'folder_id':'%%%INVALID'},{'folder_id':service.folder_id('INBOX\r\nLOGOUT')},{'q':'text\r\nLOGOUT'}):
        assert client.get('/api/mail/messages',params=params).status_code==422
    assert not imap.calls
    assert service.folder_label('&BB8EQAQ4BDIENQRC-')=='Привет'


def test_uidvalidity_reset_does_not_open_another_message(api, make_user, imap):
    response = api(make_user(admin=True)).get('/api/mail/messages/9?uidvalidity=80')
    assert response.status_code == 409
    assert not any(call[0] == 'fetch' for call in imap.calls)


def test_large_message_rejected_before_download(api, make_user, imap):
    imap.size = service.MAX_MESSAGE_BYTES + 1
    response = api(make_user(admin=True)).get('/api/mail/messages/9?uidvalidity=81')
    assert response.status_code == 413
    assert not any('BODY.PEEK[]' in str(call) for call in imap.calls)


def test_credentials_are_never_returned(api, make_user, imap):
    client = api(make_user(admin=True))
    status = client.get('/api/mail/status')
    assert status.json()['configured'] is True
    assert 'password' not in status.text
    imap.fail = True
    response = client.get('/api/mail/messages')
    assert response.status_code == 502
    assert 'private-test-password' not in response.text
    assert 'private-account' not in response.text
    assert imap.calls[-1] == ('logout',)


def test_unconfigured_mail_has_no_network_calls(api, make_user, imap, monkeypatch):
    monkeypatch.setattr(settings, 'shared_mail_enabled', False)
    client = api(make_user(admin=True))
    assert client.get('/api/mail/status').json()['configured'] is False
    assert client.get('/api/mail/messages').status_code == 503
    assert not imap.calls


@pytest.mark.parametrize('path', ['/status', '/messages', '/messages/9?uidvalidity=81', '/messages/9/attachments/2?uidvalidity=81'])
def test_anonymous_cannot_read(path):
    assert TestClient(app).get('/api/mail' + path).status_code == 401


def test_disabled_user_cannot_read(db, make_user):
    user = make_user()
    user.is_active = False
    db.commit()
    mail_app.dependency_overrides[get_db] = lambda: db
    try:
        response = TestClient(app).get('/api/mail/status', headers={'Authorization': 'Bearer ' + create_access_token(str(user.id))})
        assert response.status_code == 401
    finally:
        mail_app.dependency_overrides.clear()


@pytest.mark.parametrize('query', ['limit=51', 'before=0', 'limit=0'])
def test_invalid_pagination(api, make_user, query):
    assert api(make_user(admin=True)).get('/api/mail/messages?' + query).status_code == 422


def test_html_mail_is_plain_text_with_no_tracking():
    message = BytesParser(policy=policy.default).parsebytes(
        b'Content-Type: text/html; charset=utf-8\r\n\r\n<head><style>hidden</style></head><p>Hello</p><script>steal()</script><img src="https://tracker.example/x">')
    assert service.message_text(message) == 'Hello'


def test_unknown_encoding_and_binary_mail():
    message = BytesParser(policy=policy.default).parsebytes(b'Content-Type: text/plain; charset=unknown-bad-charset\r\n\r\nHello')
    assert service.message_text(message) == 'Hello'
    binary = BytesParser(policy=policy.default).parsebytes(b'Content-Type: application/octet-stream\r\n\r\nx')
    assert service.message_text(binary) == ''
