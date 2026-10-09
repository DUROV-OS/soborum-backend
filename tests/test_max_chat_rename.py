"""Своё название чата MAX в системе (0106) — не трогает MAX, только
отображение у нас в «Все чаты» и истории чата.
"""

from app.common.module_access import Module
from app.max import service as max_service


def _mock_list_chats(monkeypatch, chats):
    monkeypatch.setattr(max_service, "list_chats", lambda limit=None: {"count": len(chats), "chats": chats})


def _mock_get_chat(monkeypatch, result):
    monkeypatch.setattr(max_service, "get_chat", lambda chat_id, limit=50, backward=0: result)


def test_set_title_overrides_list_and_single_chat(monkeypatch, api, make_user, db):
    _mock_list_chats(monkeypatch, [{"id": -555, "title": "Числовой чат"}])
    _mock_get_chat(monkeypatch, {"chatId": -555, "title": "Числовой чат", "peer": None, "count": 0, "messages": []})
    worker = api(make_user(Module.CLIENTS))

    set_resp = worker.put("/api/max/chats/-555/title", json={"title": "Дима с объекта"})
    assert set_resp.status_code == 200, set_resp.text
    assert set_resp.json() == {"chatId": -555, "title": "Дима с объекта"}

    listed = worker.get("/api/max/chats").json()["chats"]
    assert listed[0]["title"] == "Дима с объекта"

    single = worker.get("/api/max/chats/-555").json()
    assert single["title"] == "Дима с объекта"


def test_clear_title_reverts_to_max_name(monkeypatch, api, make_user, db):
    _mock_list_chats(monkeypatch, [{"id": -555, "title": "Числовой чат"}])
    worker = api(make_user(Module.CLIENTS))

    worker.put("/api/max/chats/-555/title", json={"title": "Дима с объекта"})
    cleared = worker.delete("/api/max/chats/-555/title")
    assert cleared.status_code == 204

    listed = worker.get("/api/max/chats").json()["chats"]
    assert listed[0]["title"] == "Числовой чат"


def test_setting_title_again_replaces_previous(monkeypatch, api, make_user, db):
    _mock_list_chats(monkeypatch, [{"id": -555, "title": "Числовой чат"}])
    worker = api(make_user(Module.CLIENTS))

    worker.put("/api/max/chats/-555/title", json={"title": "Первое имя"})
    second = worker.put("/api/max/chats/-555/title", json={"title": "Второе имя"})
    assert second.status_code == 200

    listed = worker.get("/api/max/chats").json()["chats"]
    assert listed[0]["title"] == "Второе имя"


def test_empty_title_is_rejected(api, make_user, db):
    worker = api(make_user(Module.CLIENTS))

    resp = worker.put("/api/max/chats/-555/title", json={"title": "   "})
    assert resp.status_code == 400


def test_clear_without_existing_title_is_noop(api, make_user, db):
    worker = api(make_user(Module.CLIENTS))

    resp = worker.delete("/api/max/chats/-555/title")
    assert resp.status_code == 204
