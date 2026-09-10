"""Задача 0004-a: каркас режима «Совещание» — сессия, запись аудио, список."""

from app.common.module_access import Module


def test_meeting_lifecycle_and_audio(api, make_user):
    user = make_user(Module.AI)
    client = api(user)

    created = client.post("/api/ai/meetings", json={"title": "Планёрка"})
    assert created.status_code == 201
    meeting = created.json()
    assert meeting["status"] == "recording"
    assert meeting["has_audio"] is False
    assert meeting["finished_at"] is None
    assert meeting["duration_sec"] is None
    mid = meeting["id"]

    up = client.post(
        f"/api/ai/meetings/{mid}/audio",
        files={"file": ("rec.webm", b"fake-opus-bytes", "audio/webm")},
    )
    assert up.status_code == 200
    assert up.json()["has_audio"] is True

    finished = client.post(f"/api/ai/meetings/{mid}/finish")
    assert finished.status_code == 200
    body = finished.json()
    assert body["status"] == "finished"
    assert body["finished_at"] is not None
    assert body["duration_sec"] is not None and body["duration_sec"] >= 0

    listed = client.get("/api/ai/meetings")
    assert listed.status_code == 200
    assert [m["id"] for m in listed.json()] == [mid]

    detail = client.get(f"/api/ai/meetings/{mid}")
    assert detail.status_code == 200
    d = detail.json()
    assert d["audio_url"] == f"/api/ai/meetings/{mid}/audio"
    assert d["transcript"] == []
    assert d["notes"] is None
    assert d["ai_enabled"] is False  # conftest не задаёт ANTHROPIC_API_KEY

    audio = client.get(f"/api/ai/meetings/{mid}/audio")
    assert audio.status_code == 200
    assert audio.content == b"fake-opus-bytes"


def test_meeting_rename_and_delete(api, make_user):
    client = api(make_user(Module.AI))
    mid = client.post("/api/ai/meetings", json={}).json()["id"]
    client.post(
        f"/api/ai/meetings/{mid}/transcript",
        json={"lines": [{"speaker": "Спикер 1", "text": "реплика", "at_ms": 0}]},
    )

    renamed = client.patch(f"/api/ai/meetings/{mid}", json={"title": "  Планёрка по DH64  "})
    assert renamed.status_code == 200
    assert renamed.json()["title"] == "Планёрка по DH64"

    assert client.delete(f"/api/ai/meetings/{mid}").status_code == 204
    assert client.get(f"/api/ai/meetings/{mid}").status_code == 404
    assert client.get("/api/ai/meetings").json() == []
    # каскад: строки транскрипта тоже удалены
    assert (
        client.post(
            f"/api/ai/meetings/{mid}/transcript",
            json={"lines": [{"speaker": "Спикер 1", "text": "x", "at_ms": 0}]},
        ).status_code
        == 404
    )


def test_meeting_rename_delete_private_to_owner(api, make_user):
    owner = api(make_user(Module.AI))
    mid = owner.post("/api/ai/meetings", json={}).json()["id"]
    stranger = api(make_user(Module.AI))
    assert stranger.patch(f"/api/ai/meetings/{mid}", json={"title": "чужое"}).status_code == 404
    assert stranger.delete(f"/api/ai/meetings/{mid}").status_code == 404


def test_meeting_requires_ai_access(api, make_user):
    user = make_user()  # без Module.AI
    client = api(user)
    assert client.post("/api/ai/meetings", json={}).status_code == 403
    assert client.get("/api/ai/meetings").status_code == 403


def test_meeting_is_private_to_owner(api, make_user):
    owner = make_user(Module.AI)
    stranger = make_user(Module.AI)
    mid = api(owner).post("/api/ai/meetings", json={}).json()["id"]

    other = api(stranger)
    assert other.get(f"/api/ai/meetings/{mid}").status_code == 404
    assert other.post(f"/api/ai/meetings/{mid}/finish").status_code == 404
    assert other.get("/api/ai/meetings").json() == []


def test_finish_twice_conflicts(api, make_user):
    client = api(make_user(Module.AI))
    mid = client.post("/api/ai/meetings", json={}).json()["id"]
    assert client.post(f"/api/ai/meetings/{mid}/finish").status_code == 200
    assert client.post(f"/api/ai/meetings/{mid}/finish").status_code == 409


def test_audio_rejects_non_audio_file(api, make_user):
    client = api(make_user(Module.AI))
    mid = client.post("/api/ai/meetings", json={}).json()["id"]
    bad = client.post(
        f"/api/ai/meetings/{mid}/audio",
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )
    assert bad.status_code == 400


# --- 0004-b: транскрипт по репликам ----------------------------------------


def test_transcript_append_reorders_and_returns_ids(api, make_user):
    client = api(make_user(Module.AI))
    mid = client.post("/api/ai/meetings", json={}).json()["id"]

    first = client.post(
        f"/api/ai/meetings/{mid}/transcript",
        json={"lines": [
            {"speaker": "Спикер 1", "text": "Начинаем планёрку", "at_ms": 1000},
            {"speaker": "Спикер 2", "text": "  ", "at_ms": 2000},  # пустая — отбрасывается
            {"speaker": "Спикер 2", "text": "Что по монтажу", "at_ms": 3000},
        ]},
    )
    assert first.status_code == 200
    created = first.json()
    assert [c["text"] for c in created] == ["Начинаем планёрку", "Что по монтажу"]
    assert all(isinstance(c["id"], int) for c in created)

    client.post(
        f"/api/ai/meetings/{mid}/transcript",
        json={"lines": [{"speaker": "Спикер 1", "text": "Отвечаю", "at_ms": 500}]},
    )

    detail = client.get(f"/api/ai/meetings/{mid}").json()
    # порядок по at_ms: 500, 1000, 3000
    assert [l["at_ms"] for l in detail["transcript"]] == [500, 1000, 3000]
    assert detail["transcript"][0]["text"] == "Отвечаю"


def test_transcript_speaker_can_be_corrected(api, make_user):
    client = api(make_user(Module.AI))
    mid = client.post("/api/ai/meetings", json={}).json()["id"]
    line_id = client.post(
        f"/api/ai/meetings/{mid}/transcript",
        json={"lines": [{"speaker": "Спикер 1", "text": "Реплика", "at_ms": 0}]},
    ).json()[0]["id"]

    patched = client.patch(
        f"/api/ai/meetings/{mid}/transcript/{line_id}", json={"speaker": "Спикер 3"}
    )
    assert patched.status_code == 200
    assert patched.json()["speaker"] == "Спикер 3"

    other_meeting = client.post("/api/ai/meetings", json={}).json()["id"]
    assert client.patch(
        f"/api/ai/meetings/{other_meeting}/transcript/{line_id}", json={"speaker": "X"}
    ).status_code == 404


def test_transcript_frozen_after_finish(api, make_user):
    client = api(make_user(Module.AI))
    mid = client.post("/api/ai/meetings", json={}).json()["id"]
    client.post(f"/api/ai/meetings/{mid}/finish")
    late = client.post(
        f"/api/ai/meetings/{mid}/transcript",
        json={"lines": [{"speaker": "Спикер 1", "text": "поздно", "at_ms": 10}]},
    )
    assert late.status_code == 409


def test_ask_about_meeting_needs_key(api, make_user):
    # conftest очищает ANTHROPIC_API_KEY → «Спросить Марину» деградирует в 503
    client = api(make_user(Module.AI))
    mid = client.post("/api/ai/meetings", json={}).json()["id"]
    resp = client.post(f"/api/ai/meetings/{mid}/ask", json={"question": "Что решили?"})
    assert resp.status_code == 503


def test_ask_about_meeting_is_private_and_validates(api, make_user):
    owner = api(make_user(Module.AI))
    mid = owner.post("/api/ai/meetings", json={}).json()["id"]
    assert owner.post(f"/api/ai/meetings/{mid}/ask", json={"question": ""}).status_code == 422

    stranger = api(make_user(Module.AI))
    assert stranger.post(
        f"/api/ai/meetings/{mid}/ask", json={"question": "Что там?"}
    ).status_code == 404


def test_transcript_is_private_to_owner(api, make_user):
    owner = api(make_user(Module.AI))
    mid = owner.post("/api/ai/meetings", json={}).json()["id"]
    stranger = api(make_user(Module.AI))
    assert stranger.post(
        f"/api/ai/meetings/{mid}/transcript",
        json={"lines": [{"speaker": "Спикер 1", "text": "чужое", "at_ms": 0}]},
    ).status_code == 404


# --- 0004-c: заметки Марины и документ для базы знаний ---------------------


def test_notes_refresh_needs_key(api, make_user):
    # conftest очищает ANTHROPIC_API_KEY → пересчёт заметок отключён
    client = api(make_user(Module.AI))
    mid = client.post("/api/ai/meetings", json={}).json()["id"]
    resp = client.post(f"/api/ai/meetings/{mid}/notes/refresh")
    assert resp.status_code == 409
    assert resp.json()["detail"]["ai_enabled"] is False


def test_detail_notes_null_until_computed(api, make_user):
    client = api(make_user(Module.AI))
    mid = client.post("/api/ai/meetings", json={}).json()["id"]
    detail = client.get(f"/api/ai/meetings/{mid}").json()
    assert detail["notes"] is None
    assert detail["ai_enabled"] is False


def test_meeting_document_assembles_without_ai(api, make_user):
    client = api(make_user(Module.AI))
    mid = client.post("/api/ai/meetings", json={"title": "Планёрка"}).json()["id"]
    client.post(
        f"/api/ai/meetings/{mid}/transcript",
        json={"lines": [
            {"speaker": "Спикер 1", "text": "Сдвигаем монтаж", "at_ms": 1000},
            {"speaker": "Спикер 2", "text": "Ответственный Пётр", "at_ms": 4000},
        ]},
    )
    client.post(f"/api/ai/meetings/{mid}/finish")  # notes-пересчёт молча пропускается без ключа

    resp = client.get(f"/api/ai/meetings/{mid}/document")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert "attachment" in resp.headers["content-disposition"]
    body = resp.text
    assert body.startswith("---\n")
    assert "title: Планёрка" in body
    assert "## Резюме" in body
    assert "## Транскрипт" in body
    assert "Сдвигаем монтаж" in body
    assert "Ответственный Пётр" in body


def test_meeting_document_private_to_owner(api, make_user):
    owner = api(make_user(Module.AI))
    mid = owner.post("/api/ai/meetings", json={}).json()["id"]
    stranger = api(make_user(Module.AI))
    assert stranger.get(f"/api/ai/meetings/{mid}/document").status_code == 404


# --- 0004-d: реплика-обращение к Марине ------------------------------------


def test_assistant_query_line_flagged_and_kept_in_transcript(api, make_user):
    client = api(make_user(Module.AI))
    mid = client.post("/api/ai/meetings", json={}).json()["id"]
    created = client.post(
        f"/api/ai/meetings/{mid}/transcript",
        json={"lines": [
            {"speaker": "Спикер 1", "text": "Обсуждаем монтаж", "at_ms": 1000},
            {"speaker": "Спикер 1", "text": "Марина, какие риски?", "at_ms": 4000,
             "is_assistant_query": True},
        ]},
    ).json()
    assert created[0]["is_assistant_query"] is False
    assert created[1]["is_assistant_query"] is True

    detail = client.get(f"/api/ai/meetings/{mid}").json()
    # обе строки в транскрипте, флаг сохранён
    assert [l["is_assistant_query"] for l in detail["transcript"]] == [False, True]
