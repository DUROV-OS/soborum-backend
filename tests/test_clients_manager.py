"""Ответственный менеджер клиента (0080-a).

- менеджер назначается, меняется и снимается через PATCH .../manager;
- несуществующий пользователь отклоняется 400;
- те же права, что на смену стадии (require_clients_edit) — просмотра не
  достаточно;
- смена менеджера не трогает другие поля клиента (например, stage).
"""

from app.common.module_access import AccessLevel, Module


def _client(**kwargs):
    base = dict(full_name="Иван Покупателев", phone="+70000000000", email="ivan@example.com")
    base.update(kwargs)
    return base


def test_manager_can_be_set_changed_and_cleared(api, make_user):
    http = api(make_user(Module.CLIENTS, level=AccessLevel.EDIT))
    manager_a = make_user(Module.CLIENTS, level=AccessLevel.EDIT)
    manager_b = make_user(Module.CLIENTS, level=AccessLevel.EDIT)
    client = http.post("/api/clients/", json=_client()).json()
    assert client["manager_id"] is None
    assert client["manager"] is None

    set_a = http.patch(f"/api/clients/{client['id']}/manager", json={"manager_id": manager_a.id})
    assert set_a.status_code == 200, set_a.text
    assert set_a.json()["manager_id"] == manager_a.id
    assert set_a.json()["manager"]["full_name"] == manager_a.full_name

    # Перечитываем карточку — назначение сохранилось.
    reloaded = http.get(f"/api/clients/{client['id']}").json()
    assert reloaded["manager_id"] == manager_a.id

    changed = http.patch(f"/api/clients/{client['id']}/manager", json={"manager_id": manager_b.id})
    assert changed.status_code == 200, changed.text
    assert changed.json()["manager_id"] == manager_b.id
    assert changed.json()["manager"]["full_name"] == manager_b.full_name

    cleared = http.patch(f"/api/clients/{client['id']}/manager", json={"manager_id": None})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["manager_id"] is None
    assert cleared.json()["manager"] is None


def test_unknown_manager_is_rejected(api, make_user):
    http = api(make_user(Module.CLIENTS, level=AccessLevel.EDIT))
    client = http.post("/api/clients/", json=_client()).json()

    resp = http.patch(f"/api/clients/{client['id']}/manager", json={"manager_id": 424242})
    assert resp.status_code == 400
    assert "не найден" in resp.json()["detail"]


def test_manager_change_requires_edit_access(api, make_user):
    manager = make_user(Module.CLIENTS, level=AccessLevel.EDIT)
    editor = api(make_user(Module.CLIENTS, level=AccessLevel.EDIT))
    client = editor.post("/api/clients/", json=_client()).json()

    # Переавторизация следующим вызовом `api(...)` переключает overrides
    # FastAPI на нового пользователя — viewer нужно создать и использовать
    # последним, иначе запрос уйдёт от имени editor (см. соседние тесты).
    viewer = api(make_user(Module.CLIENTS, level=AccessLevel.VIEW))
    resp = viewer.patch(f"/api/clients/{client['id']}/manager", json={"manager_id": manager.id})
    assert resp.status_code == 403


def test_manager_update_does_not_touch_other_fields(api, make_user):
    http = api(make_user(Module.CLIENTS, level=AccessLevel.EDIT))
    manager = make_user(Module.CLIENTS, level=AccessLevel.EDIT)
    client = http.post("/api/clients/", json=_client()).json()
    assert client["stage"] == "lead"

    resp = http.patch(f"/api/clients/{client['id']}/manager", json={"manager_id": manager.id})
    assert resp.status_code == 200, resp.text
    assert resp.json()["stage"] == "lead"
    assert resp.json()["full_name"] == "Иван Покупателев"
