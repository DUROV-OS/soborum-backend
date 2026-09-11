"""Заметки по поставщику (задача 0011-i)."""

from app.common.module_access import Module


def _sid(client) -> int:
    return client.post("/api/warehouse/suppliers", json={"name": "ООО Заметка"}).json()["id"]


def test_add_list_and_delete_notes(api, make_user):
    user = make_user(Module.WAREHOUSE)
    client = api(user)
    sid = _sid(client)

    first = client.post(f"/api/warehouse/suppliers/{sid}/notes", json={"text": "завышает цены на метизы"})
    assert first.status_code == 201, first.text
    assert first.json()["notes"][0]["text"] == "завышает цены на метизы"
    assert first.json()["notes"][0]["author_id"] == user.id
    assert first.json()["notes"][0]["author_name"] == user.full_name

    second = client.post(f"/api/warehouse/suppliers/{sid}/notes", json={"text": "долго отвечает в MAX"})
    notes = second.json()["notes"]
    assert [n["text"] for n in notes] == ["долго отвечает в MAX", "завышает цены на метизы"]  # новые сверху

    note_id = notes[-1]["id"]
    after = client.delete(f"/api/warehouse/suppliers/{sid}/notes/{note_id}")
    assert after.status_code == 200
    assert [n["text"] for n in after.json()["notes"]] == ["долго отвечает в MAX"]


def test_empty_note_rejected(api, make_user):
    client = api(make_user(Module.WAREHOUSE))
    sid = _sid(client)
    assert client.post(f"/api/warehouse/suppliers/{sid}/notes", json={"text": "   "}).status_code == 422


def test_delete_missing_note_404(api, make_user):
    client = api(make_user(Module.WAREHOUSE))
    sid = _sid(client)
    assert client.delete(f"/api/warehouse/suppliers/{sid}/notes/999").status_code == 404
