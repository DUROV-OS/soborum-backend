"""Задача 0077: сдать задачу можно только с отчётом — комментарием исполнителя
о выполненной работе и, по желанию, файлами. Отчёт виден проверяющему в
карточке задачи и не исчезает после приёмки. Проверяющий, принимая работу или
возвращая её, тоже может оставить комментарий и приложить файлы.
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


def test_reviewer_can_attach_comment_and_file_when_accepting(db, make_user, api):
    worker = make_user(Module.TASKS)
    reviewer = make_user(Module.TASKS)
    task = _task_in_progress(db, task_service, worker, [reviewer])
    api(worker).post(f"/api/tasks/{task.id}/report", data={"comment": "Сделано"})

    response = api(reviewer).post(
        f"/api/tasks/{task.id}/review",
        data={"accept": "true", "comment": "Принято, замечаний нет"},
        files={"files": ("akt-priemki.pdf", b"%PDF-1.4 fake", "application/pdf")},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "done"
    assert [(r["kind"], r["comment"]) for r in body["reports"]] == [
        ("submission", "Сделано"),
        ("review_accepted", "Принято, замечаний нет"),
    ]
    review = body["reports"][1]
    assert review["author"]["id"] == reviewer.id
    assert [f["filename"] for f in review["files"]] == ["akt-priemki.pdf"]


def test_reviewer_comment_on_return_and_empty_decision_adds_no_record(db, make_user, api):
    worker = make_user(Module.TASKS)
    reviewer = make_user(Module.TASKS)
    task = _task_in_progress(db, task_service, worker, [reviewer])
    api(worker).post(f"/api/tasks/{task.id}/report", data={"comment": "Первая сдача"})

    returned = api(reviewer).post(
        f"/api/tasks/{task.id}/review",
        data={"accept": "false", "comment": "Переделать угол примыкания"},
    )
    assert returned.status_code == 200, returned.text
    assert returned.json()["status"] == "in_progress"
    assert returned.json()["reports"][-1]["kind"] == "review_returned"

    api(worker).post(f"/api/tasks/{task.id}/report", data={"comment": "Исправил"})
    # Принятие без комментария и файлов — запись в журнал не добавляется.
    accepted = api(reviewer).post(f"/api/tasks/{task.id}/review", data={"accept": "true", "comment": "  "})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "done"
    assert [r["kind"] for r in accepted.json()["reports"]] == [
        "submission",
        "review_returned",
        "submission",
    ]


def test_only_reviewer_of_task_on_review_can_decide(db, make_user, api):
    worker = make_user(Module.TASKS)
    reviewer = make_user(Module.TASKS)
    task = _task_in_progress(db, task_service, worker, [reviewer])

    too_early = api(reviewer).post(f"/api/tasks/{task.id}/review", data={"accept": "true", "comment": "Рано"})
    assert too_early.status_code == 400

    api(worker).post(f"/api/tasks/{task.id}/report", data={"comment": "Сделано"})
    not_reviewer = api(worker).post(
        f"/api/tasks/{task.id}/review", data={"accept": "true", "comment": "Сам себе приёмка"}
    )
    assert not_reviewer.status_code == 403
    db.expire_all()
    assert db.query(TaskReport).count() == 1


def test_author_can_edit_comment_after_task_is_accepted(db, make_user, api):
    worker = make_user(Module.TASKS)
    reviewer = make_user(Module.TASKS)
    task = _task_in_progress(db, task_service, worker, [reviewer])
    submitted = api(worker).post(f"/api/tasks/{task.id}/report", data={"comment": "Сделано"}).json()
    report_id = submitted["reports"][0]["id"]
    accepted = api(reviewer).post(f"/api/tasks/{task.id}/review", data={"accept": "true", "comment": "Принято"})
    assert accepted.json()["status"] == "done"

    response = api(worker).patch(
        f"/api/tasks/{task.id}/reports/{report_id}",
        json={"comment": "Сделано, добавил фото стыка"},
    )

    assert response.status_code == 200, response.text
    edited = response.json()["reports"][0]
    assert edited["comment"] == "Сделано, добавил фото стыка"
    assert edited["updated_at"] is not None
    assert response.json()["status"] == "done"  # правка не трогает статус


def test_only_author_edits_and_comment_cannot_be_emptied(db, make_user, api):
    worker = make_user(Module.TASKS)
    reviewer = make_user(Module.TASKS)
    task = _task_in_progress(db, task_service, worker, [reviewer])
    submitted = api(worker).post(f"/api/tasks/{task.id}/report", data={"comment": "Сделано"}).json()
    report_id = submitted["reports"][0]["id"]

    foreign = api(reviewer).patch(f"/api/tasks/{task.id}/reports/{report_id}", json={"comment": "Чужая правка"})
    assert foreign.status_code == 403

    emptied = api(worker).patch(f"/api/tasks/{task.id}/reports/{report_id}", json={"comment": "   "})
    assert emptied.status_code == 400

    missing = api(worker).patch(f"/api/tasks/{task.id}/reports/999", json={"comment": "Нет такого"})
    assert missing.status_code == 404

    db.expire_all()
    assert db.get(TaskReport, report_id).comment == "Сделано"
    assert db.get(TaskReport, report_id).updated_at is None
