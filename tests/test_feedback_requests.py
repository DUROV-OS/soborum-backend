"""Задача 0075-a: заявки «Пожелания/предложения» — права и вложения."""

import io

from app.common.module_access import Module
from app.feedback.models import FeedbackAttachmentKind, FeedbackRequest, FeedbackStatus

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


def _submit(client, *, text="Кнопка не нажимается", section="warehouse", files=None, log=None):
    data = {"text": text, "section": section}
    if log is not None:
        data["client_log"] = log
    return client.post(
        "/api/feedback/requests",
        data=data,
        files=files or [("screenshots", ("shot.png", io.BytesIO(PNG), "image/png"))],
    )


def test_worker_submits_request_with_screenshot_and_log(api, make_user, db):
    worker = make_user(Module.WAREHOUSE)

    response = _submit(api(worker), log="12:00 переход на /warehouse\n12:01 GET /api/warehouse 500")

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == FeedbackStatus.NEW.value
    assert body["section"] == "warehouse"
    assert body["author"]["id"] == worker.id
    kinds = sorted(a["kind"] for a in body["attachments"])
    assert kinds == [FeedbackAttachmentKind.LOG.value, FeedbackAttachmentKind.SCREENSHOT.value]
    assert db.query(FeedbackRequest).count() == 1


def test_request_without_log_has_only_screenshot(api, make_user):
    worker = make_user(Module.WAREHOUSE)

    body = _submit(api(worker)).json()

    assert [a["kind"] for a in body["attachments"]] == [FeedbackAttachmentKind.SCREENSHOT.value]


def test_worker_sees_only_own_requests_admin_sees_all(api, make_user):
    author = make_user(Module.WAREHOUSE)
    stranger = make_user(Module.WAREHOUSE)
    admin = make_user(admin=True)
    _submit(api(author))

    assert [r["author"]["id"] for r in api(author).get("/api/feedback/requests").json()] == [author.id]
    assert api(stranger).get("/api/feedback/requests").json() == []
    assert len(api(admin).get("/api/feedback/requests").json()) == 1


def test_stranger_cannot_open_or_download_foreign_request(api, make_user):
    author = make_user(Module.WAREHOUSE)
    stranger = make_user(Module.WAREHOUSE)
    created = _submit(api(author)).json()
    file_id = created["attachments"][0]["file_id"]

    assert api(stranger).get(f"/api/feedback/requests/{created['id']}").status_code == 403
    assert api(stranger).get(f"/api/feedback/requests/{created['id']}/files/{file_id}").status_code == 403


def test_author_and_admin_download_attachment(api, make_user):
    author = make_user(Module.WAREHOUSE)
    admin = make_user(admin=True)
    created = _submit(api(author)).json()
    file_id = created["attachments"][0]["file_id"]

    for user in (author, admin):
        response = api(user).get(f"/api/feedback/requests/{created['id']}/files/{file_id}")
        assert response.status_code == 200
        assert response.content == PNG


def test_file_of_another_request_is_not_served(api, make_user):
    admin = make_user(admin=True)
    first = _submit(api(admin)).json()
    second = _submit(api(admin)).json()
    foreign_file_id = second["attachments"][0]["file_id"]

    response = api(admin).get(f"/api/feedback/requests/{first['id']}/files/{foreign_file_id}")

    assert response.status_code == 404


def test_only_admin_changes_status(api, make_user):
    author = make_user(Module.WAREHOUSE)
    admin = make_user(admin=True)
    created = _submit(api(author)).json()

    assert api(author).patch(f"/api/feedback/requests/{created['id']}", json={"status": "done"}).status_code == 403
    response = api(admin).patch(f"/api/feedback/requests/{created['id']}", json={"status": "in_progress"})
    assert response.status_code == 200
    assert response.json()["status"] == "in_progress"


def test_non_image_attachment_is_rejected(api, make_user, db):
    worker = make_user(Module.WAREHOUSE)

    response = _submit(
        api(worker),
        files=[("screenshots", ("spec.pdf", io.BytesIO(b"%PDF-1.4"), "application/pdf"))],
    )

    assert response.status_code == 400
    assert "изображение" in response.json()["detail"]
    assert db.query(FeedbackRequest).count() == 0


def test_more_than_five_screenshots_rejected(api, make_user):
    worker = make_user(Module.WAREHOUSE)

    response = _submit(
        api(worker),
        files=[("screenshots", (f"s{i}.png", io.BytesIO(PNG), "image/png")) for i in range(6)],
    )

    assert response.status_code == 400


def test_empty_text_is_rejected(api, make_user):
    worker = make_user(Module.WAREHOUSE)

    assert _submit(api(worker), text="   ").status_code == 422
    assert _submit(api(worker), text="").status_code == 422
