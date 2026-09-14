"""Привязка клиента к чату MAX (0012): один чат — не более одного клиента,
состояние переписки живёт на связи и сбрасывается при отвязке.
"""

from app.clients import service as client_service
from app.clients.schemas import ClientCreate
from app.common.module_access import Module


def _make_client(db, name="Клиент"):
    return client_service.create_client(
        db, ClientCreate(full_name=name, phone="+70000000000", email=f"{name}@example.com")
    )


def test_one_max_chat_links_to_a_single_client(api, make_user, db):
    first = _make_client(db, "Первый")
    second = _make_client(db, "Второй")
    db.commit()
    worker = api(make_user(Module.CLIENTS))

    linked = worker.patch(f"/api/clients/{first.id}/max-chat", json={"max_chat_id": -777})
    assert linked.status_code == 200, linked.text
    assert linked.json()["max_chat_id"] == -777

    clash = worker.patch(f"/api/clients/{second.id}/max-chat", json={"max_chat_id": -777})
    assert clash.status_code == 409

    unlinked = worker.patch(f"/api/clients/{first.id}/max-chat", json={"max_chat_id": None})
    assert unlinked.status_code == 200
    assert unlinked.json()["max_chat_id"] is None

    now_ok = worker.patch(f"/api/clients/{second.id}/max-chat", json={"max_chat_id": -777})
    assert now_ok.status_code == 200


def test_chat_state_requires_a_linked_chat_and_resets_on_unlink(api, make_user, db):
    client = _make_client(db)
    db.commit()
    worker = api(make_user(Module.CLIENTS))

    no_chat = worker.patch(f"/api/clients/{client.id}/chat-state", json={"state": "waiting"})
    assert no_chat.status_code == 400

    worker.patch(f"/api/clients/{client.id}/max-chat", json={"max_chat_id": 42})
    set_state = worker.patch(f"/api/clients/{client.id}/chat-state", json={"state": "analysis"})
    assert set_state.status_code == 200, set_state.text
    assert set_state.json()["max_chat_state"] == "analysis"

    unlinked = worker.patch(f"/api/clients/{client.id}/max-chat", json={"max_chat_id": None})
    assert unlinked.json()["max_chat_state"] is None


def test_max_chat_requires_clients_module(api, make_user, db):
    client = _make_client(db)
    db.commit()
    outsider = api(make_user(Module.WAREHOUSE))

    resp = outsider.patch(f"/api/clients/{client.id}/max-chat", json={"max_chat_id": 1})
    assert resp.status_code == 403
