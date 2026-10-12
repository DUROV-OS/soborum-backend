"""Статус прочтения исходящих сообщений MAX (0101). participants чата —
{userId: время последней отметки прочтения, мс}; данные синтетические,
реальный MAX не трогаем."""

import pytest

from app.max import listener
from app.max import service as max_service

ME = "435168608"
PEER = "214427025"


def _msg(sender, time, **extra):
    return {"sender": int(sender), "id": "1", "time": time, "text": "т", "attaches": [], **extra}


def _chat(**participants):
    return {"id": 77, "type": "DIALOG", "participants": participants}


@pytest.mark.parametrize("mark, expected", [(2000, "read"), (1000, "read"), (999, "sent"), (0, "sent")])
def test_outgoing_read_when_peer_mark_not_earlier(mark, expected):
    marks = max_service._peer_read_marks(_chat(**{ME: 5000, PEER: mark}), ME)
    assert max_service._fmt_msg(_msg(ME, 1000), ME, {}, marks)["readStatus"] == expected


def test_group_message_read_if_anyone_read():
    marks = max_service._peer_read_marks(_chat(**{ME: 1, "10": 0, "11": 1500}), ME)
    assert max_service._fmt_msg(_msg(ME, 1000), ME, {}, marks)["readStatus"] == "read"


def test_incoming_system_and_saved_messages_have_no_status():
    marks = max_service._peer_read_marks(_chat(**{ME: 5000, PEER: 5000}), ME)
    assert max_service._fmt_msg(_msg(PEER, 1000), ME, {}, marks)["readStatus"] is None
    control = _msg(ME, 1000, attaches=[{"_type": "CONTROL", "event": "add"}])
    assert max_service._fmt_msg(control, ME, {}, marks)["readStatus"] is None
    saved = max_service._peer_read_marks(_chat(**{ME: 5000}), ME)  # «Избранное»: других нет
    assert max_service._fmt_msg(_msg(ME, 1000), ME, {}, saved)["readStatus"] is None


def test_unknown_marks_give_no_status():
    assert max_service._fmt_msg(_msg(ME, 1000), ME, {}, None)["readStatus"] is None


class _Session:
    def __init__(self, info=None, fail=False):
        self.info, self.fail, self.asked = info or [], fail, []

    def chat_info(self, chat_ids):
        self.asked.append(chat_ids)
        if self.fail:
            raise TimeoutError("нет ответа с opcode 48")
        return self.info


def test_marks_from_auth_chat_do_not_query_chat_info():
    s = _Session()
    assert max_service._chat_read_marks(s, 77, _chat(**{ME: 1, PEER: 5}), ME) == [5]
    assert s.asked == []


def test_chat_outside_auth_uses_chat_info():
    s = _Session(info=[_chat(**{ME: 1, PEER: 9})])
    assert max_service._chat_read_marks(s, 77, None, ME) == [9]
    assert s.asked == [[77]]


def test_chat_info_failure_means_unknown_not_error():
    assert max_service._chat_read_marks(_Session(fail=True), 77, None, ME) is None


@pytest.fixture
def published(monkeypatch):
    events = []
    monkeypatch.setattr(listener.realtime, "chat_updated", lambda chat_id: events.append(chat_id))
    return events


def test_read_notification_refreshes_chat_without_reply(published):
    replies = []
    listener.FrameHandler(replies.append).handle(
        {"ver": 11, "cmd": 0, "seq": 5, "opcode": 130,
         "payload": {"chatId": 524029541, "userId": 214427025, "mark": 1791460587185, "setAsUnread": False}}
    )
    assert published == [524029541]
    assert replies == []
