"""Поставщики (задача 0011-a): CRUD, валидация прайса, один чат MAX — один поставщик.

Раздел смонтирован под /api/warehouse, доступ — Module.WAREHOUSE.
"""

from app.common.module_access import Module


def _supplier_payload(**over):
    payload = {
        "name": "ООО Брус-Трейд",
        "categories": ["брусы/доска"],
        "contacts": [
            {"kind": "phone", "value": "+7 900 000-00-00", "person": "Иван"},
            {"kind": "email", "value": "sale@brus.example"},
        ],
    }
    payload.update(over)
    return payload


def test_create_supplier_with_contacts_and_list(api, make_user):
    client = api(make_user(Module.WAREHOUSE))

    created = client.post("/api/warehouse/suppliers", json=_supplier_payload())
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["name"] == "ООО Брус-Трейд"
    assert body["status"] == "active"
    assert len(body["contacts"]) == 2
    assert body["price_items_count"] == 0

    listed = client.get("/api/warehouse/suppliers")
    assert listed.status_code == 200
    assert [s["id"] for s in listed.json()] == [body["id"]]


def test_price_item_requires_material_and_a_priced_tier(api, make_user):
    client = api(make_user(Module.WAREHOUSE))
    sid = client.post("/api/warehouse/suppliers", json=_supplier_payload()).json()["id"]

    no_material = client.post(
        f"/api/warehouse/suppliers/{sid}/price-items",
        json={"material": "  ", "tiers": [{"min_qty": 0, "price": 100}]},
    )
    assert no_material.status_code == 422

    no_tiers = client.post(
        f"/api/warehouse/suppliers/{sid}/price-items",
        json={"material": "Доска 150×50", "tiers": []},
    )
    assert no_tiers.status_code == 422

    ok = client.post(
        f"/api/warehouse/suppliers/{sid}/price-items",
        json={
            "material": "Доска 150×50",
            "category": "брусы/доска",
            "lead_time": "10 дней",
            "round": 1,
            "tiers": [
                {"min_qty": 0, "max_qty": 100, "price": 950},
                {"min_qty": 100, "max_qty": 500, "price": 900},
                {"min_qty": 500, "max_qty": None, "price": 850},
            ],
        },
    )
    assert ok.status_code == 201, ok.text
    supplier = ok.json()
    assert supplier["price_items_count"] == 1
    item = supplier["price_items"][0]
    assert item["material"] == "Доска 150×50"
    assert len(item["tiers"]) == 3
    assert item["updated_at"]


def test_update_price_item_rejects_dropping_all_prices(api, make_user):
    client = api(make_user(Module.WAREHOUSE))
    sid = client.post("/api/warehouse/suppliers", json=_supplier_payload()).json()["id"]
    item_id = client.post(
        f"/api/warehouse/suppliers/{sid}/price-items",
        json={"material": "Брус 100×100", "tiers": [{"min_qty": 0, "price": 500}]},
    ).json()["price_items"][0]["id"]

    bad = client.patch(
        f"/api/warehouse/suppliers/{sid}/price-items/{item_id}",
        json={"tiers": []},
    )
    assert bad.status_code == 422

    good = client.patch(
        f"/api/warehouse/suppliers/{sid}/price-items/{item_id}",
        json={"lead_time": "3 дня"},
    )
    assert good.status_code == 200
    assert good.json()["price_items"][0]["lead_time"] == "3 дня"


def test_one_max_chat_links_to_a_single_supplier(api, make_user):
    client = api(make_user(Module.WAREHOUSE))
    first = client.post("/api/warehouse/suppliers", json=_supplier_payload(name="Первый")).json()["id"]
    second = client.post("/api/warehouse/suppliers", json=_supplier_payload(name="Второй")).json()["id"]

    linked = client.post(f"/api/warehouse/suppliers/{first}/link-max-chat", json={"chat_id": -777})
    assert linked.status_code == 200
    assert linked.json()["max_chat_id"] == -777

    clash = client.post(f"/api/warehouse/suppliers/{second}/link-max-chat", json={"chat_id": -777})
    assert clash.status_code == 409

    unlinked = client.delete(f"/api/warehouse/suppliers/{first}/link-max-chat")
    assert unlinked.status_code == 200
    assert unlinked.json()["max_chat_id"] is None

    now_ok = client.post(f"/api/warehouse/suppliers/{second}/link-max-chat", json={"chat_id": -777})
    assert now_ok.status_code == 200


def test_supplier_access_requires_warehouse_module(api, make_user):
    client = api(make_user(Module.CLIENTS))
    assert client.get("/api/warehouse/suppliers").status_code == 403
