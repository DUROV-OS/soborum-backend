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
