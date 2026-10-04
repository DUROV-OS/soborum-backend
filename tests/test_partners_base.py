"""База партнёров (0083-a).

- партнёр заводится с категорией и обязательным городом;
- список фильтруется по категории и городу (без учёта регистра) и ищется по
  имени, организации и цифрам телефона;
- город и имя нельзя ни опустить, ни стереть правкой;
- доступ — по разделу «Клиенты»: просмотр читает, редактирование пишет,
  удаление — только полный доступ.
"""

from app.common.module_access import AccessLevel, Module


def _partner(**kwargs):
    base = dict(category="REALTOR", name="Пётр Риэлторов", city="Москва", phone="+7 900 123-45-67")
    base.update(kwargs)
    return base


def test_create_partner_trims_city(api, make_user):
    client = api(make_user(Module.CLIENTS, level=AccessLevel.EDIT))
    resp = client.post("/api/partners/", json=_partner(city="  Москва  ", organization="Агентство №1"))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["category"] == "REALTOR"
    assert body["city"] == "Москва"
    assert body["organization"] == "Агентство №1"
    assert body["created_by_id"] is not None


def test_city_and_name_are_required(api, make_user):
    client = api(make_user(Module.CLIENTS, level=AccessLevel.EDIT))
    assert client.post("/api/partners/", json=_partner(city="   ")).status_code == 422
    no_city = _partner()
    no_city.pop("city")
    assert client.post("/api/partners/", json=no_city).status_code == 422
    assert client.post("/api/partners/", json=_partner(name="")).status_code == 422
    assert client.post("/api/partners/", json=_partner(category="FRIEND")).status_code == 422


def test_filters_by_category_and_city(api, make_user):
    client = api(make_user(Module.CLIENTS, level=AccessLevel.EDIT))
    client.post("/api/partners/", json=_partner())
    client.post("/api/partners/", json=_partner(name="ООО Торговля", category="COMMERCE", city="Казань"))
    client.post("/api/partners/", json=_partner(name="Земля и Ко", category="LAND_SPECIALIST", city="москва"))

    realtors = client.get("/api/partners/", params={"category": "REALTOR", "city": "МОСКВА"}).json()
    assert [p["name"] for p in realtors] == ["Пётр Риэлторов"]
    assert client.get("/api/partners/", params={"category": "COMMERCE", "city": "Москва"}).json() == []
    moscow = client.get("/api/partners/", params={"city": "москва"}).json()
    assert {p["name"] for p in moscow} == {"Пётр Риэлторов", "Земля и Ко"}

    # Один город в разном регистре — одна строка фильтра.
    assert client.get("/api/partners/cities").json() == ["Казань", "Москва"]


def test_search_by_name_organization_and_phone(api, make_user):
    client = api(make_user(Module.CLIENTS, level=AccessLevel.EDIT))
    client.post("/api/partners/", json=_partner(organization="Этажи"))
    client.post("/api/partners/", json=_partner(name="Анна Землякова", phone="89997776655"))

    def names(search):
        return [p["name"] for p in client.get("/api/partners/", params={"search": search}).json()]

    assert names("риэлтор") == ["Пётр Риэлторов"]
    assert names("этаж") == ["Пётр Риэлторов"]
    assert names("+7 999 777-66-55") == ["Анна Землякова"]
    assert names("4567") == ["Пётр Риэлторов"]


def test_update_changes_fields_but_not_clears_required(api, make_user):
    client = api(make_user(Module.CLIENTS, level=AccessLevel.EDIT))
    partner = client.post("/api/partners/", json=_partner()).json()

    resp = client.patch(f"/api/partners/{partner['id']}", json={"city": "Тула", "phone": "", "category": "COMMERCE"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["city"], body["phone"], body["category"]) == ("Тула", None, "COMMERCE")
    assert body["name"] == "Пётр Риэлторов"

    assert client.patch(f"/api/partners/{partner['id']}", json={"city": " "}).status_code == 422
    assert client.patch(f"/api/partners/{partner['id']}", json={"city": None}).json()["city"] == "Тула"
    assert client.patch("/api/partners/999999", json={"city": "Тула"}).status_code == 404


def test_notes_are_added_and_shown_in_card(api, make_user):
    client = api(make_user(Module.CLIENTS, level=AccessLevel.EDIT))
    partner = client.post("/api/partners/", json=_partner()).json()
    resp = client.post(f"/api/partners/{partner['id']}/notes", json={"text": "комиссия 3%"})
    assert resp.status_code == 201
    assert client.post(f"/api/partners/{partner['id']}/notes", json={"text": "  "}).status_code == 422

    card = client.get(f"/api/partners/{partner['id']}").json()
    assert [n["text"] for n in card["notes"]] == ["комиссия 3%"]


def test_access_follows_clients_section(api, make_user):
    editor = api(make_user(Module.CLIENTS, level=AccessLevel.EDIT))
    partner = editor.post("/api/partners/", json=_partner()).json()
    note = editor.post(f"/api/partners/{partner['id']}/notes", json={"text": "условия"}).json()

    outsider = api(make_user(Module.WAREHOUSE, level=AccessLevel.FULL))
    assert outsider.get("/api/partners/").status_code == 403

    viewer = api(make_user(Module.CLIENTS, level=AccessLevel.VIEW))
    assert viewer.get(f"/api/partners/{partner['id']}").status_code == 200
    assert viewer.post("/api/partners/", json=_partner()).status_code == 403

    # Редактор не удаляет ни партнёра, ни заметку — это полный доступ.
    assert editor.delete(f"/api/partners/{partner['id']}/notes/{note['id']}").status_code == 403
    assert editor.delete(f"/api/partners/{partner['id']}").status_code == 403

    owner = api(make_user(Module.CLIENTS, level=AccessLevel.FULL))
    assert owner.delete(f"/api/partners/{partner['id']}/notes/{note['id']}").status_code == 204
    assert owner.delete(f"/api/partners/{partner['id']}").status_code == 204
    assert owner.get(f"/api/partners/{partner['id']}").status_code == 404
