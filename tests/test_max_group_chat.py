"""MAX: в групповом чате исходящие/входящие и служебные сообщения размечены верно.

Проверяем нормализацию в app/max/service.py без реального websocket —
подменяем session() фейком с данными группового чата.
"""

import contextlib

import pytest

from app.max import service


class _FakeSession:
    VIEWER = "47422938"  # id текущего пользователя в MAX

    CONTACTS = [
        {"id": 47422938, "names": [{"name": "Я"}]},
        {"id": 96795400, "names": [{"name": "Виктория"}]},
        {"id": 150135457, "names": [{"name": "Алексей"}]},
    ]

    CHAT = {"id": -78479694648660, "type": "CHAT", "title": "Проект BARN64"}

    HISTORY = [
        {"sender": 96795400, "id": "1", "time": 10, "text": "Когда монтаж?"},
        {"sender": 96795400, "id": "2", "time": 11, "text": "Ждём ответа"},
        {"sender": 47422938, "id": "3", "time": 12, "text": "На следующей неделе"},
        {"sender": 150135457, "id": "4", "time": 13, "text": "",
         "attaches": [{"_type": "CONTROL", "event": "joinByLink", "userId": 150135457}]},
        {"sender": 150135457, "id": "5", "time": 14, "text": "Здравствуйте"},
    ]

    def contacts_by_id(self):
        return {str(c["id"]): c for c in self.CONTACTS}

    def viewer_id(self):
        return self.VIEWER

    def chats(self):
        return [self.CHAT]

    def history(self, chat_id, forward=50, backward=0):
        return list(self.HISTORY)


@pytest.fixture
def fake_session(monkeypatch):
    @contextlib.contextmanager
    def _session():
        yield _FakeSession()

    monkeypatch.setattr(service, "session", _session)


def test_group_chat_direction_and_author(fake_session):
    res = service.get_chat(_FakeSession.CHAT["id"], limit=80)

    assert res["isGroup"] is True
    msgs = {m["id"]: m for m in res["messages"]}

    # входящие — от других участников, с именем автора
    assert msgs["1"]["isOutgoing"] is False
    assert msgs["1"]["senderName"] == "Виктория"
    assert msgs["1"]["senderId"] == "96795400"

    # исходящее — совпал id участника, а не «потому что 1:1»
    assert msgs["3"]["isOutgoing"] is True
    assert msgs["3"]["senderId"] == _FakeSession.VIEWER

    # порядок сообщений сохранён
    assert [m["id"] for m in res["messages"]] == ["1", "2", "3", "4", "5"]


def test_group_chat_system_message_is_service_not_outgoing(fake_session):
    res = service.get_chat(_FakeSession.CHAT["id"], limit=80)
    joined = next(m for m in res["messages"] if m["id"] == "4")

    assert joined["isSystem"] is True
    assert joined["isOutgoing"] is False
    assert joined["systemText"] == "присоединился по ссылке"


def test_dialog_is_not_group():
    assert service._is_group_chat({"type": "DIALOG"}) is False
    assert service._is_group_chat({"type": "CHAT"}) is True
    assert service._is_group_chat(None) is False


def test_fmt_msg_without_contacts_keeps_direction():
    """Даже без справочника контактов признак стороны не теряется."""
    mine = service._fmt_msg({"sender": 5, "id": "1", "time": 1, "text": "hi"}, viewer_id="5")
    theirs = service._fmt_msg({"sender": 9, "id": "2", "time": 2, "text": "yo"}, viewer_id="5")

    assert mine["isOutgoing"] is True
    assert mine["senderName"] is None
    assert theirs["isOutgoing"] is False
