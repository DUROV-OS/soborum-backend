"""Задача 0084-g: правка отчёта о сдаче не подменяет принятое доказательство.
Прежний текст сохраняется ревизией, правка после приёмки помечается;
дополнять отчёт после приёмки по-прежнему можно
(test_author_can_edit_comment_after_task_is_accepted).
"""
from app.common.module_access import Module
from app.tasks import service as task_service
from app.tasks.models import TaskReportRevision, TaskStatus


def _submitted(db, api, worker, reviewer, comment="Сделал X"):
    task = task_service.create_task(
        db, title="Утеплить мансарду", assignee_ids=[worker.id], reviewer_ids=[reviewer.id],
    )
    db.flush()
    task_service.set_status(db, task, TaskStatus.IN_PROGRESS, worker)
    db.commit()
    body = api(worker).post(f"/api/tasks/{task.id}/report", data={"comment": comment}).json()
    return task, body["reports"][0]["id"]


def _edit(api, user, task, report_id, comment):
    return api(user).patch(f"/api/tasks/{task.id}/reports/{report_id}", json={"comment": comment})


def test_edit_after_acceptance_keeps_accepted_text_in_history(db, make_user, api):
    worker = make_user(Module.TASKS)
    reviewer = make_user(Module.TASKS)
    task, report_id = _submitted(db, api, worker, reviewer)
    api(reviewer).post(f"/api/tasks/{task.id}/review", data={"accept": "true"})

    response = _edit(api, worker, task, report_id, "Сделал X и Y")

    assert response.status_code == 200, response.text
    report = response.json()["reports"][0]
    assert report["comment"] == "Сделал X и Y"
    assert report["revisions_count"] == 1
    assert report["edited_after_acceptance"] is True

    history = api(reviewer).get(f"/api/tasks/{task.id}/reports/{report_id}/revisions")
    assert history.status_code == 200, history.text
    [revision] = history.json()
    assert revision["comment"] == "Сделал X"
    assert revision["edited_by"]["id"] == worker.id
    assert revision["after_acceptance"] is True
    assert revision["edited_at"]


def test_edit_before_acceptance_is_marked_as_plain_edit(db, make_user, api):
    worker = make_user(Module.TASKS)
    reviewer = make_user(Module.TASKS)
    task, report_id = _submitted(db, api, worker, reviewer)

    report = _edit(api, worker, task, report_id, "Сделал X, опечатку поправил").json()["reports"][0]

    assert report["revisions_count"] == 1
    assert report["edited_after_acceptance"] is False
    [revision] = api(worker).get(f"/api/tasks/{task.id}/reports/{report_id}/revisions").json()
    assert revision["comment"] == "Сделал X"
    assert revision["after_acceptance"] is False


def test_every_edit_adds_a_revision_in_order_and_unchanged_text_adds_none(db, make_user, api):
    worker = make_user(Module.TASKS)
    reviewer = make_user(Module.TASKS)
    task, report_id = _submitted(db, api, worker, reviewer)

    _edit(api, worker, task, report_id, "Вторая версия")
    api(reviewer).post(f"/api/tasks/{task.id}/review", data={"accept": "true"})
    _edit(api, worker, task, report_id, "Третья версия")
    same = _edit(api, worker, task, report_id, "Третья версия").json()["reports"][0]

    assert same["revisions_count"] == 2
    assert same["edited_after_acceptance"] is True
    history = api(worker).get(f"/api/tasks/{task.id}/reports/{report_id}/revisions").json()
    assert [(r["comment"], r["after_acceptance"]) for r in history] == [
        ("Сделал X", False),
        ("Вторая версия", True),
    ]


def test_rejected_edit_leaves_no_revision(db, make_user, api):
    worker = make_user(Module.TASKS)
    reviewer = make_user(Module.TASKS)
    task, report_id = _submitted(db, api, worker, reviewer)

    assert _edit(api, reviewer, task, report_id, "Чужая правка").status_code == 403
    assert _edit(api, worker, task, report_id, "   ").status_code == 400

    assert db.query(TaskReportRevision).count() == 0


def test_unedited_report_has_no_revisions(db, make_user, api):
    worker = make_user(Module.TASKS)
    reviewer = make_user(Module.TASKS)
    task, report_id = _submitted(db, api, worker, reviewer)

    report = api(worker).get(f"/api/tasks/{task.id}").json()["reports"][0]

    assert report["revisions_count"] == 0
    assert report["edited_after_acceptance"] is False
    assert api(worker).get(f"/api/tasks/{task.id}/reports/{report_id}/revisions").json() == []


def test_revisions_of_foreign_or_missing_report_are_404(db, make_user, api):
    worker = make_user(Module.TASKS)
    reviewer = make_user(Module.TASKS)
    task, report_id = _submitted(db, api, worker, reviewer)
    other, _ = _submitted(db, api, worker, reviewer, comment="Другая задача")

    assert api(worker).get(f"/api/tasks/{other.id}/reports/{report_id}/revisions").status_code == 404
    assert api(worker).get(f"/api/tasks/{task.id}/reports/999/revisions").status_code == 404


def test_revisions_need_tasks_view_access(db, make_user, api):
    worker = make_user(Module.TASKS)
    reviewer = make_user(Module.TASKS)
    outsider = make_user(Module.CLIENTS)
    task, report_id = _submitted(db, api, worker, reviewer)

    response = api(outsider).get(f"/api/tasks/{task.id}/reports/{report_id}/revisions")

    assert response.status_code == 403


def test_revisions_have_no_write_endpoints(db, make_user, api):
    worker = make_user(Module.TASKS)
    reviewer = make_user(Module.TASKS)
    task, report_id = _submitted(db, api, worker, reviewer)
    _edit(api, worker, task, report_id, "Вторая версия")
    url = f"/api/tasks/{task.id}/reports/{report_id}/revisions"

    assert api(worker).post(url, json={"comment": "подмена"}).status_code == 405
    assert api(worker).delete(url).status_code == 405
    assert db.query(TaskReportRevision).one().comment == "Сделал X"
