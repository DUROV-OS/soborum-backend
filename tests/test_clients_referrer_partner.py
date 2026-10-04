"""«Кто рекомендовал» у клиента (0083-c).

- при создании клиента рекомендателем выбирается партнёр из базы партнёров;
- рекомендатель меняется и снимается через изменение источника;
- у партнёра видно приведённых им клиентов;
- несуществующий партнёр отклоняется, а партнёра-рекомендателя нельзя удалить,
  иначе у клиентов молча пропадёт «чей он».
"""

from app.common.module_access import AccessLevel, Module


def _client(**kwargs):
    base = dict(full_name="Иван Покупателев", phone="+70000000000", email="ivan@example.com")
    base.update(kwargs)
    return base


def _partner(api_client, name="Пётр Риэлторов"):
    resp = api_client.post("/api/partners/", json={"category": "REALTOR", "name": name, "city": "Москва"})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_client_created_with_referrer(api, make_user):
    http = api(make_user(Module.CLIENTS, level=AccessLevel.FULL))
    realtor = _partner(http)

    resp = http.post("/api/clients/", json=_client(referrer_partner_id=realtor["id"]))
    assert resp.status_code == 201, resp.text
    assert resp.json()["referrer"] == {
        "id": realtor["id"],
        "name": "Пётр Риэлторов",
        "category": "REALTOR",
        "city": "Москва",
        "organization": None,
    }

    referred = http.get(f"/api/partners/{realtor['id']}/clients").json()
    assert [c["full_name"] for c in referred] == ["Иван Покупателев"]
    assert referred[0]["stage"] == "lead"


def test_client_without_referrer_has_none(api, make_user):
    http = api(make_user(Module.CLIENTS, level=AccessLevel.EDIT))
    assert http.post("/api/clients/", json=_client()).json()["referrer"] is None


def test_unknown_referrer_is_rejected(api, make_user):
    http = api(make_user(Module.CLIENTS, level=AccessLevel.EDIT))
    resp = http.post("/api/clients/", json=_client(referrer_partner_id=424242))
    assert resp.status_code == 400
    assert "не найден" in resp.json()["detail"]


def test_referrer_changed_and_cleared_via_source(api, make_user):
    http = api(make_user(Module.CLIENTS, level=AccessLevel.EDIT))
    first = _partner(http, "Первый")
    second = _partner(http, "Второй")
    client = http.post("/api/clients/", json=_client(referrer_partner_id=first["id"])).json()

    moved = http.patch(f"/api/clients/{client['id']}/source", json={"referrer_partner_id": second["id"]})
    assert moved.status_code == 200, moved.text
    assert moved.json()["referrer"]["name"] == "Второй"
    assert http.get(f"/api/partners/{first['id']}/clients").json() == []
    assert len(http.get(f"/api/partners/{second['id']}/clients").json()) == 1

    cleared = http.patch(f"/api/clients/{client['id']}/source", json={"referrer_partner_id": None})
    assert cleared.json()["referrer"] is None


def test_referrer_is_independent_of_legacy_agency_text(api, make_user):
    """Старые текстовые поля агентства (0079-c) живут рядом с рекомендателем."""
    http = api(make_user(Module.CLIENTS, level=AccessLevel.EDIT))
    realtor = _partner(http)
    client = http.post(
        "/api/clients/",
        json=_client(via_agency=True, agency_name="Агентство №1", referrer_partner_id=realtor["id"]),
    ).json()
    assert client["agency_name"] == "Агентство №1"
    assert client["referrer"]["id"] == realtor["id"]


def test_referrer_partner_cannot_be_deleted(api, make_user):
    http = api(make_user(Module.CLIENTS, level=AccessLevel.FULL))
    realtor = _partner(http)
    client = http.post("/api/clients/", json=_client(referrer_partner_id=realtor["id"])).json()

    resp = http.delete(f"/api/partners/{realtor['id']}")
    assert resp.status_code == 409
    assert "1" in resp.json()["detail"]

    http.patch(f"/api/clients/{client['id']}/source", json={"referrer_partner_id": None})
    assert http.delete(f"/api/partners/{realtor['id']}").status_code == 204


def test_referred_clients_follow_view_access(api, make_user):
    editor = api(make_user(Module.CLIENTS, level=AccessLevel.EDIT))
    realtor = _partner(editor)
    outsider = api(make_user(Module.WAREHOUSE, level=AccessLevel.FULL))
    assert outsider.get(f"/api/partners/{realtor['id']}/clients").status_code == 403
    viewer = api(make_user(Module.CLIENTS, level=AccessLevel.VIEW))
    assert viewer.get(f"/api/partners/{realtor['id']}/clients").status_code == 200
    assert editor.get("/api/partners/999999/clients").status_code == 404
