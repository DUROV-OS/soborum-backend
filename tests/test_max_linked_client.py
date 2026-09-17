"""GET /api/max/chats аннотирует каждый чат данными о привязанном клиенте
(0012) — обратная привязка «из MAX к клиенту» показывает, что чат уже занят.
"""

from app.clients import service as client_service
from app.clients.schemas import ClientChatLinkCreate, ClientCreate
from app.common.module_access import Module
from app.max import service as max_service


def _make_client(db, name="Клиент", chat_id=None):
    client = client_service.create_client(
        db, ClientCreate(full_name=name, phone="+70000000000", email=f"{name}@example.com")
    )
    if chat_id is not None:
        client_service.create_chat_link(db, client, ClientChatLinkCreate(max_chat_id=chat_id, label="Клиент"))
    return client


def test_chat_list_annotates_linked_client(monkeypatch, api, make_user, db):
    monkeypatch.setattr(
        max_service,
        "list_chats",
        lambda limit=None: {
            "count": 2,
            "chats": [
                {"id": -777, "title": "Клиентский чат"},
                {"id": 5, "title": "Не привязан"},
            ],
        },
    )
    client = _make_client(db, "Иван", chat_id=-777)
    db.commit()
    worker = api(make_user(Module.CLIENTS))

    res = worker.get("/api/max/chats")
    assert res.status_code == 200, res.text
    chats = {c["id"]: c for c in res.json()["chats"]}

    assert chats[-777]["linkedClientId"] == client.id
    assert chats[-777]["linkedClientName"] == "Иван"
    assert chats[5]["linkedClientId"] is None
    assert chats[5]["linkedClientName"] is None
