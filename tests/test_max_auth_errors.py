import json
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from app.max import client

class Socket:
    def __init__(self, reply):
        self.frames = [{'opcode':6,'seq':1,'cmd':1,'payload':{}}, {'opcode':19,'seq':2, **reply}]
        self.closed = False
    def settimeout(self, value): pass
    def connect(self, *args, **kwargs): pass
    def send(self, value): pass
    def recv(self): return json.dumps(self.frames.pop(0))
    def close(self): self.closed = True

@pytest.mark.parametrize('reply', [
    {'cmd':3,'payload':{'error':'login.token','localizedMessage':'DO_NOT_LOG_TOKEN'}},
    {'cmd':1,'payload':{'error':'DO_NOT_LOG_TOKEN'}},
    {'cmd':1,'payload':{'chats':[]}},
    {'cmd':1,'payload':None},
    {'cmd':1,'payload':['invalid']},
    {'cmd':1,'payload':{'profile':{'id':0},'chats':[]}},
    {'cmd':1,'payload':{'profile':{'id':'invalid'},'chats':[]}},
    {'cmd':2,'payload':{'profile':{'id':1},'chats':[]}},
])
def test_rejected_auth_is_not_empty_success_and_does_not_leak_secrets(monkeypatch, reply):
    socket = Socket(reply)
    monkeypatch.setattr(client, 'websocket', SimpleNamespace(WebSocket=lambda:socket))
    with pytest.raises(HTTPException) as error: client.MaxSession('DO_NOT_LOG_TOKEN').open()
    assert error.value.status_code == 502
    assert 'DO_NOT_LOG_TOKEN' not in error.value.detail
    assert socket.closed

def test_confirmed_empty_account_remains_valid(monkeypatch):
    socket = Socket({'cmd':1,'payload':{'profile':{'id':1},'chats':[]}})
    monkeypatch.setattr(client, 'websocket', SimpleNamespace(WebSocket=lambda:socket))
    session = client.MaxSession('synthetic-token').open()
    assert session.chats() == []
    session.close(); assert socket.closed

def test_rejected_hello_never_attempts_auth(monkeypatch):
    socket = Socket({})
    socket.frames[0] = {'opcode':6,'seq':1,'cmd':3,'payload':{'error':'DO_NOT_LOG_TOKEN'}}
    sent = []
    socket.send = lambda value: sent.append(json.loads(value)['opcode'])
    monkeypatch.setattr(client, 'websocket', SimpleNamespace(WebSocket=lambda:socket))
    with pytest.raises(HTTPException) as error: client.MaxSession('DO_NOT_LOG_TOKEN').open()
    assert error.value.status_code == 502 and 'DO_NOT_LOG_TOKEN' not in error.value.detail
    assert sent == [6] and socket.closed

def test_socket_failure_is_closed_and_does_not_leak_provider_details(monkeypatch):
    socket = Socket({})
    def fail(*args, **kwargs): raise RuntimeError('DO_NOT_LOG_TOKEN')
    socket.connect = fail
    monkeypatch.setattr(client, 'websocket', SimpleNamespace(WebSocket=lambda:socket))
    monkeypatch.setattr(client.settings, 'max_token', 'synthetic-token')
    with pytest.raises(HTTPException) as error:
        with client.session(): pytest.fail('Unauthenticated session yielded')
    assert error.value.status_code == 502 and 'DO_NOT_LOG_TOKEN' not in error.value.detail
    assert socket.closed
