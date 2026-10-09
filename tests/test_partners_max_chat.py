"""Привязка партнёра к чату MAX (0105) — по образцу привязки клиента (0053),
но чат не может быть привязан и к партнёру, и к клиенту одновременно.
"""

from app.clients import service as client_service
from app.clients.schemas import ClientChatLinkCreate, ClientCreate
from app.common.module_access import Module
from app.max import service as max_service
from app.partners import service as partner_service
from app.partners.schemas import PartnerChatLinkCreate, PartnerCreate


def _make_partner(db, name="Партнёр", chat_id=None):
    partner = partner_service.create_partner(
        db, PartnerCreate(category="REALTOR", name=name, city="Москва"), created_by_id=None
    )
    if chat_id is not None:
        partner_service.create_chat_link(db, partner, PartnerChatLinkCreate(max_chat_id=chat_id, label="С партнёром"))
    return partner


def _make_client(db, name="Клиент", chat_id=None):
    client = client_service.create_client(
        db, ClientCreate(full_name=name, phone="+70000000000", email=f"{name}@example.com")
    )
    if chat_id is not None:
        client_service.create_chat_link(db, client, ClientChatLinkCreate(max_chat_id=chat_id, label="С клиентом"))
    return client


def test_partner_can_be_linked_to_chat(api, make_user, db):
    partner = _make_partner(db)
    db.commit()
    worker = api(make_user(Module.CLIENTS))

    resp = worker.post(f"/api/partners/{partner.id}/chat-links", json={"max_chat_id": -555, "label": "С партнёром"})
    assert resp.status_code == 201, resp.text

    fetched = worker.get(f"/api/partners/{partner.id}")
    links = fetched.json()["chat_links"]
    assert [l["max_chat_id"] for l in links] == [-555]


def test_chat_already_linked_to_another_partner_is_rejected(api, make_user, db):
    first = _make_partner(db, "Первый", chat_id=-555)
    second = _make_partner(db, "Второй")
    db.commit()
    worker = api(make_user(Module.CLIENTS))

    clash = worker.post(f"/api/partners/{second.id}/chat-links", json={"max_chat_id": -555, "label": "Чат"})
    assert clash.status_code == 409
    assert "Первый" in clash.json()["detail"]


def test_chat_already_linked_to_client_is_rejected_for_partner(api, make_user, db):
    _make_client(db, "Иван", chat_id=-555)
    partner = _make_partner(db)
    db.commit()
    worker = api(make_user(Module.CLIENTS))

    clash = worker.post(f"/api/partners/{partner.id}/chat-links", json={"max_chat_id": -555, "label": "Чат"})
    assert clash.status_code == 409
    assert "Иван" in clash.json()["detail"]


def test_chat_already_linked_to_partner_is_rejected_for_client(api, make_user, db):
    _make_partner(db, "Риэлтор", chat_id=-555)
    client = _make_client(db, "Клиент")
    db.commit()
    worker = api(make_user(Module.CLIENTS))

    clash = worker.post(f"/api/clients/{client.id}/chat-links", json={"max_chat_id": -555, "label": "Чат"})
    assert clash.status_code == 409
    assert "Риэлтор" in clash.json()["detail"]


def test_empty_label_is_rejected(api, make_user, db):
    partner = _make_partner(db)
    db.commit()
    worker = api(make_user(Module.CLIENTS))

    resp = worker.post(f"/api/partners/{partner.id}/chat-links", json={"max_chat_id": 42, "label": "   "})
    assert resp.status_code == 400


def test_max_chat_link_requires_clients_module(api, make_user, db):
    partner = _make_partner(db)
    db.commit()
    outsider = api(make_user(Module.WAREHOUSE))

    resp = outsider.post(f"/api/partners/{partner.id}/chat-links", json={"max_chat_id": 1, "label": "Чат"})
    assert resp.status_code == 403


def test_chat_list_annotates_linked_partner(monkeypatch, api, make_user, db):
    monkeypatch.setattr(
        max_service,
        "list_chats",
        lambda limit=None: {
            "count": 2,
            "chats": [
                {"id": -555, "title": "Партнёрский чат"},
                {"id": 5, "title": "Не привязан"},
            ],
        },
    )
    partner = _make_partner(db, "Риэлтор", chat_id=-555)
    db.commit()
    worker = api(make_user(Module.CLIENTS))

    res = worker.get("/api/max/chats")
    assert res.status_code == 200, res.text
    chats = {c["id"]: c for c in res.json()["chats"]}

    assert chats[-555]["linkedPartnerId"] == partner.id
    assert chats[-555]["linkedPartnerName"] == "Риэлтор"
    assert chats[-555]["linkedClientId"] is None
    assert chats[5]["linkedPartnerId"] is None
