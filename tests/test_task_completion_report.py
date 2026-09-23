"""Задача 0077: сдать задачу можно только с отчётом — комментарием исполнителя
о выполненной работе и, по желанию, файлами. Отчёт виден проверяющему в
карточке задачи и не исчезает после приёмки.
"""
from app.common.files import FileAsset, FilePurpose
from app.common.module_access import Module
from app.tasks import service as task_service
from app.tasks.models import TaskReport, TaskStatus


def _task_in_progress(db, task_service_, worker, reviewers=()):
    task = task_service_.create_task(
        db,
        title="Покрасить фасад",
        assignee_ids=[worker.id],
        reviewer_ids=[r.id for r in reviewers],
    )
    db.flush()
    task_service_.set_status(db, task, TaskStatus.IN_PROGRESS, worker)
    db.commit()
    return task


def test_report_moves_task_to_review_and_stores_comment_and_file(db, make_user, api):
    worker = make_user(Module.TASKS)
    reviewer = make_user(Module.TASKS)
    task = _task_in_progress(db, task_service, worker, [reviewer])

    response = api(worker).post(
        f"/api/tasks/{task.id}/report",
        data={"comment": "Фасад покрашен, второй слой сохнет"},
        files={"files": ("akt.pdf", b"%PDF-1.4 fake", "application/pdf")},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "in_review"
    assert len(body["reports"]) == 1
    report = body["reports"][0]
    assert report["comment"] == "Фасад покрашен, второй слой сохнет"
    assert report["author"]["id"] == worker.id
    assert [f["filename"] for f in report["files"]] == ["akt.pdf"]

    asset = db.get(FileAsset, report["files"][0]["id"])
    assert asset.purpose == FilePurpose.TASK_REPORT_FILE


def test_report_without_files_is_allowed_and_empty_comment_is_not(db, make_user, api):
    worker = make_user(Module.TASKS)
    reviewer = make_user(Module.TASKS)
    task = _task_in_progress(db, task_service, worker, [reviewer])
    client = api(worker)

    blank = client.post(f"/api/tasks/{task.id}/report", data={"comment": "   "})
    assert blank.status_code == 400
    db.expire_all()
    assert db.get(type(task), task.id).status == TaskStatus.IN_PROGRESS
    assert db.query(TaskReport).count() == 0

    ok = client.post(f"/api/tasks/{task.id}/report", data={"comment": "Сделано"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["reports"][0]["files"] == []


def test_only_assignee_in_progress_can_report(db, make_user, api):
    worker = make_user(Module.TASKS)
    reviewer = make_user(Module.TASKS)
    task = _task_in_progress(db, task_service, worker, [reviewer])

    foreign = api(reviewer).post(f"/api/tasks/{task.id}/report", data={"comment": "Я не исполнитель"})
    assert foreign.status_code == 403

    api(worker).post(f"/api/tasks/{task.id}/report", data={"comment": "Сделано"})
    again = api(worker).post(f"/api/tasks/{task.id}/report", data={"comment": "Ещё раз"})
    assert again.status_code == 400  # задача уже на проверке
    assert db.query(TaskReport).count() == 1


def test_status_endpoint_no_longer_accepts_in_review(db, make_user, api):
    worker = make_user(Module.TASKS)
    reviewer = make_user(Module.TASKS)
    task = _task_in_progress(db, task_service, worker, [reviewer])

    response = api(worker).patch(f"/api/tasks/{task.id}/status", json={"status": "in_review"})

    assert response.status_code == 400
    assert "отчёт" in response.json()["detail"]
    db.expire_all()
    assert db.get(type(task), task.id).status == TaskStatus.IN_PROGRESS


def test_second_report_after_return_and_survives_acceptance(db, make_user, api):
    worker = make_user(Module.TASKS)
    reviewer = make_user(Module.TASKS)
    task = _task_in_progress(db, task_service, worker, [reviewer])

    api(worker).post(f"/api/tasks/{task.id}/report", data={"comment": "Первая сдача"})
    api(reviewer).patch(f"/api/tasks/{task.id}/status", json={"status": "in_progress"})
    api(worker).post(f"/api/tasks/{task.id}/report", data={"comment": "Исправил замечания"})
    accepted = api(reviewer).patch(f"/api/tasks/{task.id}/status", json={"status": "done"})

    assert accepted.status_code == 200, accepted.text
    body = accepted.json()
    assert body["status"] == "done"
    assert [r["comment"] for r in body["reports"]] == ["Первая сдача", "Исправил замечания"]


def test_task_without_reviewers_closes_immediately_keeping_report(db, make_user, api):
    worker = make_user(Module.TASKS)
    task = _task_in_progress(db, task_service, worker)

    response = api(worker).post(f"/api/tasks/{task.id}/report", data={"comment": "Сдал без проверки"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "done"
    assert [r["comment"] for r in body["reports"]] == ["Сдал без проверки"]
