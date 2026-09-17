"""Привязка клиента к чатам MAX (0053): один клиент — несколько чатов, но
каждый чат по-прежнему принадлежит не более чем одному клиенту; отвязка одной
привязки не трогает остальные привязки клиента.
"""

from app.clients import service as client_service
from app.clients.schemas import ClientCreate
from app.common.module_access import Module


def _make_client(db, name="Клиент"):
    return client_service.create_client(
        db, ClientCreate(full_name=name, phone="+70000000000", email=f"{name}@example.com")
    )


def test_client_can_have_several_chat_links(api, make_user, db):
    client = _make_client(db)
    db.commit()
    worker = api(make_user(Module.CLIENTS))

    first = worker.post(f"/api/clients/{client.id}/chat-links", json={"max_chat_id": -777, "label": "С клиентом"})
    assert first.status_code == 201, first.text
    second = worker.post(f"/api/clients/{client.id}/chat-links", json={"max_chat_id": -888, "label": "С помощником"})
    assert second.status_code == 201, second.text

    fetched = worker.get(f"/api/clients/{client.id}")
    links = fetched.json()["chat_links"]
    assert {l["max_chat_id"] for l in links} == {-777, -888}
    assert {l["label"] for l in links} == {"С клиентом", "С помощником"}


def test_chat_already_linked_to_another_client_is_rejected(api, make_user, db):
    first = _make_client(db, "Первый")
    second = _make_client(db, "Второй")
    db.commit()
    worker = api(make_user(Module.CLIENTS))

    linked = worker.post(f"/api/clients/{first.id}/chat-links", json={"max_chat_id": -777, "label": "С клиентом"})
    assert linked.status_code == 201, linked.text

    clash = worker.post(f"/api/clients/{second.id}/chat-links", json={"max_chat_id": -777, "label": "Мой чат"})
    assert clash.status_code == 409

    still_first = worker.get(f"/api/clients/{first.id}").json()
    assert len(still_first["chat_links"]) == 1


def test_empty_label_is_rejected(api, make_user, db):
    client = _make_client(db)
    db.commit()
    worker = api(make_user(Module.CLIENTS))

    resp = worker.post(f"/api/clients/{client.id}/chat-links", json={"max_chat_id": 42, "label": "   "})
    assert resp.status_code == 400


def test_unlinking_one_chat_does_not_touch_the_others(api, make_user, db):
    client = _make_client(db)
    db.commit()
    worker = api(make_user(Module.CLIENTS))

    a = worker.post(f"/api/clients/{client.id}/chat-links", json={"max_chat_id": 1, "label": "С клиентом"}).json()
    b = worker.post(f"/api/clients/{client.id}/chat-links", json={"max_chat_id": 2, "label": "С помощником"}).json()

    deleted = worker.delete(f"/api/clients/{client.id}/chat-links/{b['id']}")
    assert deleted.status_code == 204

    remaining = worker.get(f"/api/clients/{client.id}").json()["chat_links"]
    assert len(remaining) == 1
    assert remaining[0]["id"] == a["id"]
    assert remaining[0]["state"] is None


def test_chat_link_state_update(api, make_user, db):
    client = _make_client(db)
    db.commit()
    worker = api(make_user(Module.CLIENTS))

    link = worker.post(f"/api/clients/{client.id}/chat-links", json={"max_chat_id": 42, "label": "С клиентом"}).json()

    updated = worker.patch(
        f"/api/clients/{client.id}/chat-links/{link['id']}", json={"state": "analysis"}
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["state"] == "analysis"


def test_max_chat_requires_clients_module(api, make_user, db):
    client = _make_client(db)
    db.commit()
    outsider = api(make_user(Module.WAREHOUSE))

    resp = outsider.post(f"/api/clients/{client.id}/chat-links", json={"max_chat_id": 1, "label": "Чат"})
    assert resp.status_code == 403
