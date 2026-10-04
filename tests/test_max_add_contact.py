"""MAX: «Новый контакт» — поиск по номеру, добавление и открытие диалога (0093).

Сервис и роутер — с поддельной сессией (реальный MAX не трогаем); разбор
ответов сервера — на MaxSession с поддельным websocket.
"""

import contextlib
import json

import pytest

from app.common.module_access import Module
from app.max import service
from app.max.client import ContactError, MaxSession

VIEWER = "47422938"
KNOWN = {"id": 96795400, "names": [{"name": "Виктория из контактов"}]}
STRANGER = {"id": 150135457, "names": [{"name": "Профиль MAX"}]}
PHONES = {"79001112233": KNOWN, "79004445566": STRANGER, "79990000000": {"id": int(VIEWER)}}


class _FakeSession:
    def __init__(self):
        self.contacts = {str(KNOWN["id"]): KNOWN}
        self.lookups = []
        self.added = []

    def viewer_id(self):
        return VIEWER

    def contacts_by_id(self):
        return dict(self.contacts)

    def contact_by_phone(self, phone):
        self.lookups.append(phone)
        if phone not in PHONES:
            raise ContactError("Не найдено", code="not.found")
        return PHONES[phone]

    def add_contact(self, phone, first_name, last_name=None):
        self.added.append((phone, first_name, last_name))
        names = [{"firstName": first_name, "lastName": last_name}]
        contact = {"id": PHONES[phone]["id"], "names": names}
        self.contacts[str(contact["id"])] = contact
        return {"contact": contact, "new": True}

    def chats(self):
        return []

    def history(self, chat_id, forward=50, backward=0):
        return []


@pytest.fixture
def fake(monkeypatch):
    fake = _FakeSession()

    @contextlib.contextmanager
    def _session():
        yield fake

    monkeypatch.setattr(service, "session", _session)
    return fake


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("+7 (900) 444-55-66", "79004445566"),
        ("8 900 444 55 66", "79004445566"),
        ("9004445566", "79004445566"),
        ("375291234567", "375291234567"),
    ],
)
def test_normalize_phone(raw, expected):
    assert service.normalize_phone(raw) == expected


def test_new_contact_is_added_and_dialog_id_is_xor(fake, api, make_user):
    worker = api(make_user(Module.WAREHOUSE))
    resp = worker.post(
        "/api/max/contacts", json={"phone": "8 900 444-55-66", "first_name": " Тест ", "last_name": "Приёмка"}
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert fake.lookups == ["79004445566"]
    assert fake.added == [("79004445566", "Тест", "Приёмка")]
    assert body == {
        "chatId": STRANGER["id"] ^ int(VIEWER),
        "contactId": str(STRANGER["id"]),
        "name": "Тест Приёмка",
        "alreadyContact": False,
    }


def test_existing_contact_is_not_renamed(fake, api, make_user):
    worker = api(make_user(Module.WAREHOUSE))
    resp = worker.post("/api/max/contacts", json={"phone": "+79001112233", "first_name": "Другое имя"})
    assert resp.status_code == 201, resp.text
    assert fake.added == []
    assert resp.json()["alreadyContact"] is True
    assert resp.json()["name"] == "Виктория из контактов"
    assert resp.json()["chatId"] == KNOWN["id"] ^ int(VIEWER)


def test_unknown_number_is_404_and_nothing_added(fake, api, make_user):
    worker = api(make_user(Module.WAREHOUSE))
    resp = worker.post("/api/max/contacts", json={"phone": "+7 911 000-00-00", "first_name": "Кто-то"})
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Этот номер не зарегистрирован в MAX"
    assert fake.added == []


@pytest.mark.parametrize(
    "payload, detail",
    [
        ({"phone": "12345", "first_name": "Имя"}, "Некорректный номер телефона"),
        ({"phone": "+79990000000", "first_name": "Я"}, "Это номер аккаунта организации"),
        ({"phone": "+79004445566", "first_name": "   "}, "Укажите имя контакта"),
    ],
)
def test_rejected_without_adding(fake, api, make_user, payload, detail):
    worker = api(make_user(Module.WAREHOUSE))
    resp = worker.post("/api/max/contacts", json=payload)
    assert resp.status_code == 422
    assert resp.json()["detail"] == detail
    assert fake.added == []


def test_new_dialog_without_messages_is_titled_by_contact(fake):
    """Диалога ещё нет в списке чатов — шапка берёт имя из контакта."""
    service.start_dialog("79004445566", "Тест", "Приёмка")
    res = service.get_chat(STRANGER["id"] ^ int(VIEWER))
    assert res["title"] == "Тест Приёмка"
    assert res["messages"] == []
    assert res["isGroup"] is False


class _FakeWs:
    def __init__(self, reply_payload, cmd):
        self.reply_payload, self.cmd, self.sent = reply_payload, cmd, []

    def send(self, raw):
        self.sent.append(json.loads(raw))

    def recv(self):
        last = self.sent[-1]
        return json.dumps({"opcode": last["opcode"], "seq": last["seq"], "cmd": self.cmd, "payload": self.reply_payload})


def _session_with(reply_payload, cmd=1):
    s = MaxSession("token")
    s.ws = _FakeWs(reply_payload, cmd)
    return s


def test_client_lookup_sends_opcode_46_and_parses_contact():
    s = _session_with({"contact": {"id": 5, "names": []}})
    assert s.contact_by_phone("79004445566") == {"id": 5, "names": []}
    assert s.ws.sent[-1]["opcode"] == 46
    assert s.ws.sent[-1]["payload"] == {"phone": "79004445566"}


def test_client_not_found_raises_with_code():
    s = _session_with({"error": "not.found", "localizedMessage": "Не найдено"}, cmd=3)
    with pytest.raises(ContactError) as exc:
        s.contact_by_phone("79110000000")
    assert exc.value.code == "not.found"


def test_client_add_contact_sends_opcode_41_with_names():
    s = _session_with({"contact": {"id": 5}, "new": True})
    assert s.add_contact("79004445566", "Тест", "Приёмка") == {"contact": {"id": 5}, "new": True}
    assert s.ws.sent[-1]["opcode"] == 41
    assert s.ws.sent[-1]["payload"] == {"phone": "79004445566", "firstName": "Тест", "lastName": "Приёмка"}
