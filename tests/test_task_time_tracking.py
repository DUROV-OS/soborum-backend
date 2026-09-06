"""app.tasks.timelog: every status change is logged with its timestamp, and a
per-task rollup adds up how long the task was actually worked on. The Tasks API
and task behaviour are unchanged - this is a side-effect-only audit trail for
employee KPI and AI task-duration estimation.
"""
from datetime import datetime, timedelta, timezone

from app.common.module_access import Module
from app.tasks import service as task_service
from app.tasks import timelog
from app.tasks.models import TaskStageEvent, TaskStatus, TaskWorkDuration


def _events(db, task_id):
    return (
        db.query(TaskStageEvent)
        .filter(TaskStageEvent.task_id == task_id)
        .order_by(TaskStageEvent.created_at, TaskStageEvent.id)
        .all()
    )


def _backdate(db, task_id, offsets):
    """Pin each event's created_at to now + offset (seconds) so durations are
    deterministic, then recompute the rollup from the log."""
    base = datetime.now(timezone.utc)
    events = _events(db, task_id)
    for event, seconds in zip(events, offsets):
        event.created_at = base + timedelta(seconds=seconds)
    db.flush()


def test_creation_is_logged(db, make_user):
    make_user(Module.TASKS)
    task = task_service.create_task(db, title="Смонтировать каркас")
    db.commit()

    events = _events(db, task.id)
    assert len(events) == 1
    assert events[0].from_status is None
    assert events[0].to_status == TaskStatus.READY
    assert events[0].automatic is True


def test_full_lifecycle_records_every_stage_and_totals(db, make_user):
    worker = make_user(Module.TASKS)
    reviewer = make_user(Module.TASKS)
    task = task_service.create_task(
        db, title="Покрасить фасад", assignee_ids=[worker.id], reviewer_ids=[reviewer.id]
    )
    db.flush()

    task_service.set_status(db, task, TaskStatus.IN_PROGRESS, worker)
    task_service.set_status(db, task, TaskStatus.IN_REVIEW, worker)
    task_service.set_status(db, task, TaskStatus.DONE, reviewer)
    db.commit()

    events = _events(db, task.id)
    transitions = [(e.from_status, e.to_status) for e in events]
    assert transitions == [
        (None, TaskStatus.READY),
        (TaskStatus.READY, TaskStatus.IN_PROGRESS),
        (TaskStatus.IN_PROGRESS, TaskStatus.IN_REVIEW),
        (TaskStatus.IN_REVIEW, TaskStatus.DONE),
    ]
    # actor attribution for KPI
    assert events[1].actor_id == worker.id
    assert events[3].actor_id == reviewer.id

    # ready 100s, in_progress 300s, in_review 60s
    _backdate(db, task.id, [0, 100, 400, 460])
    timelog._recompute(db, task)

    row = db.get(TaskWorkDuration, task.id)
    assert row.is_complete is True
    assert row.seconds_by_status == {"ready": 100, "in_progress": 300, "in_review": 60}
    assert row.total_working_seconds == 360  # in_progress + in_review
    assert row.total_lead_seconds == 460
    assert row.first_started_at is not None
    assert row.completed_at is not None


def test_rollup_is_partial_while_in_progress(db, make_user):
    worker = make_user(Module.TASKS)
    task = task_service.create_task(db, title="Залить фундамент", assignee_ids=[worker.id])
    db.flush()
    task_service.set_status(db, task, TaskStatus.IN_PROGRESS, worker)
    db.commit()

    _backdate(db, task.id, [0, 120])
    timelog._recompute(db, task)

    row = db.get(TaskWorkDuration, task.id)
    assert row.is_complete is False
    assert row.completed_at is None
    assert row.total_lead_seconds == 0
    assert row.seconds_by_status == {"ready": 120}


def test_auto_done_without_reviewers_is_marked_automatic(db, make_user):
    worker = make_user(Module.TASKS)
    task = task_service.create_task(db, title="Вывезти мусор", assignee_ids=[worker.id])
    db.flush()
    task_service.set_status(db, task, TaskStatus.IN_PROGRESS, worker)
    task_service.set_status(db, task, TaskStatus.IN_REVIEW, worker)
    db.commit()

    events = _events(db, task.id)
    assert events[-1].to_status == TaskStatus.DONE
    assert events[-1].actor_id is None
    assert events[-1].automatic is True


def test_force_close_is_logged(db, make_user):
    make_user(Module.TASKS)
    task = task_service.create_task(db, title="Согласовать смету")
    db.flush()
    task_service.force_close(db, task)
    db.commit()

    events = _events(db, task.id)
    assert events[-1].from_status == TaskStatus.READY
    assert events[-1].to_status == TaskStatus.DONE
    assert events[-1].automatic is True


def test_dependency_cascade_logs_ready_transition(db, make_user):
    make_user(Module.TASKS)
    blocker = task_service.create_task(db, title="Завезти материалы")
    dependent = task_service.create_task(db, title="Начать монтаж", depends_on_ids=[blocker.id])
    db.flush()
    assert dependent.status == TaskStatus.NOT_READY

    task_service.force_close(db, blocker)
    db.commit()

    dep_events = _events(db, dependent.id)
    assert [(e.from_status, e.to_status) for e in dep_events] == [
        (None, TaskStatus.NOT_READY),
        (TaskStatus.NOT_READY, TaskStatus.READY),
    ]
    assert dep_events[-1].automatic is True
