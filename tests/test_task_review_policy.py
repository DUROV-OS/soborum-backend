"""Задача 0084-f: задачи блоков производства не закрываются сдачей без
приёмки. Политика — app/tasks/policy.py::REVIEW_POLICY; прочие задачи без
проверяющих по-прежнему закрываются сразу (см.
test_task_without_reviewers_closes_immediately_keeping_report и
test_auto_done_without_reviewers_is_marked_automatic).
"""
from app.common.module_access import Module
from app.tasks import policy
from app.tasks import service as task_service
from app.tasks.models import TaskReport, TaskReportKind, TaskStageEvent, TaskStatus

# Как в test_tasks_scope: sqlite в тестах не проверяет внешние ключи, а для
# политики важен только сам факт block_id — производство целиком не нужно.
BLOCK_ID = 999


def _block_task_in_progress(db, worker, *, reviewers=(), responsible=None):
    task = task_service.create_task(
        db,
        title="Смонтировать стропильную систему",
        assignee_ids=[worker.id],
        reviewer_ids=[r.id for r in reviewers],
        responsible_id=responsible.id if responsible else None,
        block_id=BLOCK_ID,
    )
    db.flush()
    task_service.set_status(db, task, TaskStatus.IN_PROGRESS, worker)
    db.commit()
    return task


def test_policy_table_production_requires_review_everything_else_auto_closes():
    assert policy.REVIEW_POLICY == {
        policy.TaskKind.PRODUCTION_BLOCK: policy.ReviewPolicy.REVIEW_REQUIRED,
        policy.TaskKind.OTHER: policy.ReviewPolicy.AUTO_CLOSE_ALLOWED,
    }


def test_block_task_without_reviewer_and_responsible_stays_in_review(db, make_user, api):
    worker = make_user(Module.TASKS)
    task = _block_task_in_progress(db, worker)

    response = api(worker).post(f"/api/tasks/{task.id}/report", data={"comment": "Стропила стоят"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "in_review"
    assert body["review_policy"] == "review_required"
    assert body["review_blocked_reason"] == "no_reviewer"
    assert body["reviewers"] == []
    assert [r["kind"] for r in body["reports"]] == ["submission"]
    last_event = (
        db.query(TaskStageEvent).filter(TaskStageEvent.task_id == task.id)
        .order_by(TaskStageEvent.id.desc()).first()
    )
    assert last_event.to_status == TaskStatus.IN_REVIEW


def test_stuck_block_task_closes_after_reviewer_is_assigned_and_accepts(db, make_user, api):
    worker = make_user(Module.TASKS)
    reviewer = make_user(Module.TASKS)
    task = _block_task_in_progress(db, worker)
    api(worker).post(f"/api/tasks/{task.id}/report", data={"comment": "Стропила стоят"})

    assigned = api(reviewer).patch(f"/api/tasks/{task.id}", json={"reviewer_ids": [reviewer.id]})
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["review_blocked_reason"] is None

    accepted = api(reviewer).post(f"/api/tasks/{task.id}/review", data={"accept": "true"})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "done"


def test_responsible_becomes_reviewer_with_journal_record(db, make_user, api):
    worker = make_user(Module.TASKS)
    responsible = make_user(Module.TASKS)
    responsible.full_name = "Борис Ответственный"
    db.commit()
    task = _block_task_in_progress(db, worker, responsible=responsible)

    response = api(worker).post(f"/api/tasks/{task.id}/report", data={"comment": "Стропила стоят"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "in_review"
    assert [r["id"] for r in body["reviewers"]] == [responsible.id]
    assert body["review_blocked_reason"] is None
    assert [r["kind"] for r in body["reports"]] == ["submission", "reviewer_assigned"]
    assert body["reports"][1]["comment"] == (
        "Проверяющий назначен по политике: ответственный — Борис Ответственный"
    )

    accepted = api(responsible).post(f"/api/tasks/{task.id}/review", data={"accept": "true"})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "done"


def test_responsible_who_is_the_assignee_is_not_made_reviewer(db, make_user, api):
    worker = make_user(Module.TASKS)
    task = _block_task_in_progress(db, worker, responsible=worker)

    body = api(worker).post(f"/api/tasks/{task.id}/report", data={"comment": "Сделал"}).json()

    assert body["status"] == "in_review"
    assert body["reviewers"] == []
    assert body["review_blocked_reason"] == "no_reviewer"
    assert db.query(TaskReport).filter(TaskReport.kind == TaskReportKind.REVIEWER_ASSIGNED).count() == 0


def test_assignee_cannot_accept_own_block_task(db, make_user, api):
    worker = make_user(Module.TASKS)
    task = _block_task_in_progress(db, worker, reviewers=[worker])
    submitted = api(worker).post(f"/api/tasks/{task.id}/report", data={"comment": "Сделал"}).json()
    # Единственный проверяющий — сам исполнитель: принять задачу некому.
    assert submitted["review_blocked_reason"] == "no_reviewer"

    via_review = api(worker).post(
        f"/api/tasks/{task.id}/review", data={"accept": "true", "comment": "Сам себя принял"},
    )
    via_status = api(worker).patch(f"/api/tasks/{task.id}/status", json={"status": "done"})

    assert via_review.status_code == 403
    assert via_status.status_code == 403
    db.expire_all()
    assert db.get(type(task), task.id).status == TaskStatus.IN_REVIEW
    assert db.query(TaskReport).filter(TaskReport.kind == TaskReportKind.REVIEW_ACCEPTED).count() == 0


def test_assignee_only_reviewer_is_backed_up_by_responsible(db, make_user, api):
    worker = make_user(Module.TASKS)
    responsible = make_user(Module.TASKS)
    task = _block_task_in_progress(db, worker, reviewers=[worker], responsible=responsible)

    body = api(worker).post(f"/api/tasks/{task.id}/report", data={"comment": "Сделал"}).json()

    assert sorted(r["id"] for r in body["reviewers"]) == sorted([worker.id, responsible.id])
    assert body["review_blocked_reason"] is None


def test_policy_reviewer_record_cannot_be_edited(db, make_user, api):
    worker = make_user(Module.TASKS)
    responsible = make_user(Module.TASKS)
    task = _block_task_in_progress(db, worker, responsible=responsible)
    body = api(worker).post(f"/api/tasks/{task.id}/report", data={"comment": "Сделал"}).json()
    record = body["reports"][1]

    response = api(worker).patch(
        f"/api/tasks/{task.id}/reports/{record['id']}", json={"comment": "Проверять не нужно"},
    )

    assert response.status_code == 400
    db.expire_all()
    assert db.get(TaskReport, record["id"]).comment.startswith("Проверяющий назначен по политике")


def test_regular_task_without_reviewers_still_closes_on_submit(db, make_user, api):
    worker = make_user(Module.TASKS)
    task = task_service.create_task(db, title="Заказать воду в офис", assignee_ids=[worker.id])
    db.flush()
    task_service.set_status(db, task, TaskStatus.IN_PROGRESS, worker)
    db.commit()

    body = api(worker).post(f"/api/tasks/{task.id}/report", data={"comment": "Заказал"}).json()

    assert body["status"] == "done"
    assert body["review_policy"] == "auto_close_allowed"
    assert body["review_blocked_reason"] is None


def test_regular_task_assignee_reviewer_can_still_accept(db, make_user, api):
    worker = make_user(Module.TASKS)
    task = task_service.create_task(
        db, title="Заказать воду в офис", assignee_ids=[worker.id], reviewer_ids=[worker.id],
    )
    db.flush()
    task_service.set_status(db, task, TaskStatus.IN_PROGRESS, worker)
    db.commit()
    api(worker).post(f"/api/tasks/{task.id}/report", data={"comment": "Заказал"})

    accepted = api(worker).post(f"/api/tasks/{task.id}/review", data={"accept": "true"})

    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "done"
