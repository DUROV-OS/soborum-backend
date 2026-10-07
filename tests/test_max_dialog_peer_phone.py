"""MAX (0099): номер собеседника личного диалога — из контактов аккаунта.

MAX отдаёт ``phone`` только у контактов аккаунта; у того, кто написал сам и не
сохранён в контактах, номера нет. Проверяем нормализацию в app/max/service.py
без реального websocket — подменяем session() фейком.
"""

import contextlib

import pytest

from app.max import service

VIEWER = 1000
KNOWN = 2000  # сохранён в контактах аккаунта — номер есть
STRANGER = 3000  # написал сам, в контактах нет — номера нет
GROUP_ID = -78479694648660


class _FakeSession:
    CONTACTS = [
        {"id": VIEWER, "names": [{"name": "Организация"}], "phone": 79990000000},
        {"id": KNOWN, "names": [{"name": "Иван Петров"}], "phone": "79001234567"},
    ]

    CHATS = [
        {"id": KNOWN ^ VIEWER, "type": "DIALOG", "participants": {str(VIEWER): 0, str(KNOWN): 0}},
        {"id": STRANGER ^ VIEWER, "type": "DIALOG", "participants": {str(VIEWER): 0, str(STRANGER): 0}},
        {"id": GROUP_ID, "type": "CHAT", "title": "Проект BARN64"},
    ]

    def contacts_by_id(self):
        return {str(c["id"]): c for c in self.CONTACTS}

    def viewer_id(self):
        return str(VIEWER)

    def chats(self):
        return list(self.CHATS)

    def last_messages(self):
        return {}

    def history(self, chat_id, forward=50, backward=0):
        return []


@pytest.fixture
def fake_session(monkeypatch):
    @contextlib.contextmanager
    def _session():
        yield _FakeSession()

    monkeypatch.setattr(service, "session", _session)


def test_dialog_with_account_contact_has_phone(fake_session):
    res = service.get_chat(KNOWN ^ VIEWER)

    assert res["peer"] == {"contactId": str(KNOWN), "name": "Иван Петров", "phone": "+79001234567"}


def test_dialog_with_stranger_has_no_phone(fake_session):
    res = service.get_chat(STRANGER ^ VIEWER)

    assert res["peer"] == {"contactId": str(STRANGER), "name": None, "phone": None}


def test_group_chat_and_favorites_have_no_peer(fake_session):
    assert service.get_chat(GROUP_ID)["peer"] is None
    assert service.get_chat(0)["peer"] is None


def test_chat_list_phone_only_for_known_dialog(fake_session):
    chats = {c["id"]: c for c in service.list_chats()["chats"]}

    assert chats[KNOWN ^ VIEWER]["phone"] == "+79001234567"
    assert chats[STRANGER ^ VIEWER]["phone"] is None
    assert chats[GROUP_ID]["phone"] is None
