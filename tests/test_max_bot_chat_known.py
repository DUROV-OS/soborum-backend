"""Привязки к чатам старого аккаунта MAX помечены как неизвестные боту
(0082-c), профиль бота для подсказки «напишите боту». Значения синтетические.
"""

import pytest

from app.clients import service as client_service
from app.clients.schemas import ClientCreate
from app.common.module_access import Module
from app.core.config import settings
from app.max import bot_api
from app.max import service as max_service
from app.max.models import MaxBotChat


@pytest.fixture(autouse=True)
def _reset_profile(monkeypatch):
    monkeypatch.setattr(max_service, "_bot_profile", None)


def _bot_chat(db, chat_id):
    db.add(MaxBotChat(chat_id=chat_id, type="dialog", title="Иван", status="active", unread=0))
    db.commit()


def test_client_link_to_old_account_chat_is_not_known(api, make_user, db):
    client = client_service.create_client(
        db, ClientCreate(full_name="Иван", phone="+70000000000", email="ivan@example.com")
    )
    db.commit()
    _bot_chat(db, 555)
    worker = api(make_user(Module.CLIENTS))

    old = worker.post(f"/api/clients/{client.id}/chat-links", json={"max_chat_id": -777, "label": "Старый"})
    assert old.status_code == 201, old.text
    assert old.json()["bot_chat_known"] is False
    new = worker.post(f"/api/clients/{client.id}/chat-links", json={"max_chat_id": 555, "label": "Бот"})
    assert new.json()["bot_chat_known"] is True

    links = {l["max_chat_id"]: l["bot_chat_known"] for l in worker.get(f"/api/clients/{client.id}").json()["chat_links"]}
    assert links == {-777: False, 555: True}


def test_supplier_chat_known_flag_follows_relink(api, make_user, db):
    _bot_chat(db, 555)
    worker = api(make_user(Module.WAREHOUSE))
    sid = worker.post("/api/warehouse/suppliers", json={"name": "Пиломатериалы"}).json()["id"]

    assert worker.get(f"/api/warehouse/suppliers/{sid}").json()["max_chat_bot_known"] is False
    old = worker.post(f"/api/warehouse/suppliers/{sid}/link-max-chat", json={"chat_id": -777})
    assert old.status_code == 200, old.text
    assert old.json()["max_chat_bot_known"] is False
    new = worker.post(f"/api/warehouse/suppliers/{sid}/link-max-chat", json={"chat_id": 555})
    assert new.json()["max_chat_bot_known"] is True


def test_bot_profile_is_cached_with_link(monkeypatch, api, make_user):
    calls = []
    monkeypatch.setattr(settings, "max_bot_token", "test-token")
    monkeypatch.setattr(
        bot_api, "get_me",
        lambda: calls.append(1) or {"user_id": 1000, "name": "Марина", "username": "marina_bot", "is_bot": True},
    )
    worker = api(make_user(Module.CLIENTS))

    for _ in range(2):
        res = worker.get("/api/max/bot")
        assert res.status_code == 200, res.text
        assert res.json() == {"name": "Марина", "username": "marina_bot", "link": "https://max.ru/marina_bot"}
    assert calls == [1]


def test_bot_profile_without_token_is_503(monkeypatch, api, make_user):
    monkeypatch.setattr(settings, "max_bot_token", "")
    res = api(make_user(Module.CLIENTS)).get("/api/max/bot")
    assert res.status_code == 503
