"""app.tasks.story_points_stats.workload_by_user + GET /api/tasks/workload
(0070-f): агрегаты по сотруднику для вкладки «Сотрудники» раздела «Задачи»
(открытые задачи/очки, тенденция за неделю, просрочки, общая загруженность),
доступ только администратору.
"""

from datetime import datetime, timedelta, timezone

from app.common.module_access import Module
from app.tasks import service as task_service
from app.tasks import timelog
from app.tasks.models import Task, TaskStageEvent, TaskStatus
from app.tasks.schemas import WorkloadLevel
from app.tasks.story_points_stats import WORKLOAD_LOW_MAX, WORKLOAD_MEDIUM_MAX, workload_by_user


def _finish(db, task, worker):
    task_service.set_status(db, task, TaskStatus.IN_PROGRESS, worker)
    task_service.set_status(db, task, TaskStatus.IN_REVIEW, worker)
    db.flush()


def _backdate_completion(db, task_id, completed_at):
    events = (
        db.query(TaskStageEvent)
        .filter(TaskStageEvent.task_id == task_id)
        .order_by(TaskStageEvent.created_at, TaskStageEvent.id)
        .all()
    )
    shift = completed_at - events[-1].created_at.replace(tzinfo=timezone.utc)
    for event in events:
        event.created_at = event.created_at.replace(tzinfo=timezone.utc) + shift
    db.flush()
    task = db.get(Task, task_id)
    timelog._recompute(db, task)


def test_workload_counts_open_tasks_points_and_overdue(db, make_user):
    worker = make_user(Module.TASKS)
    now = datetime.now(timezone.utc)

    overdue = task_service.create_task(db, title="Просрочена", assignee_ids=[worker.id], deadline=now - timedelta(days=1))
    overdue.story_points = 3
    on_time = task_service.create_task(db, title="В срок", assignee_ids=[worker.id], deadline=now + timedelta(days=5))
    on_time.story_points = 2
    db.commit()

    rows = workload_by_user(db, now=now)
    row = next(r for r in rows if r.user_id == worker.id)
    assert row.open_tasks_count == 2
    assert row.open_story_points == 5
    assert row.overdue_count == 1
    assert row.full_name == worker.full_name


def test_workload_trend_splits_last_week_from_week_before(db, make_user):
    worker = make_user(Module.TASKS)
    now = datetime.now(timezone.utc)

    recent = task_service.create_task(db, title="Закрыта на этой неделе", assignee_ids=[worker.id])
    recent.story_points = 5
    _finish(db, recent, worker)
    _backdate_completion(db, recent.id, now - timedelta(days=2))

    older = task_service.create_task(db, title="Закрыта неделей раньше", assignee_ids=[worker.id])
    older.story_points = 3
    _finish(db, older, worker)
    _backdate_completion(db, older.id, now - timedelta(days=10))

    db.commit()

    rows = workload_by_user(db, now=now)
    row = next(r for r in rows if r.user_id == worker.id)
    assert row.completed_points_7d == 5
    assert row.completed_points_prev_7d == 3


def test_workload_excludes_user_with_no_tasks_ever(db, make_user):
    make_user(Module.TASKS)  # ни одной задачи вообще
    active = make_user(Module.TASKS)
    task_service.create_task(db, title="Задача", assignee_ids=[active.id])
    db.commit()

    rows = workload_by_user(db)
    assert [r.user_id for r in rows] == [active.id]


def test_workload_level_thresholds(db, make_user):
    low = make_user(Module.TASKS)
    task_service.create_task(db, title="Низкая", assignee_ids=[low.id]).story_points = WORKLOAD_LOW_MAX

    medium = make_user(Module.TASKS)
    task_service.create_task(db, title="Средняя", assignee_ids=[medium.id]).story_points = WORKLOAD_LOW_MAX + 1

    high = make_user(Module.TASKS)
    task_service.create_task(db, title="Высокая", assignee_ids=[high.id]).story_points = WORKLOAD_MEDIUM_MAX + 1

    db.commit()

    rows = {r.user_id: r for r in workload_by_user(db)}
    assert rows[low.id].workload_level == WorkloadLevel.LOW
    assert rows[medium.id].workload_level == WorkloadLevel.MEDIUM
    assert rows[high.id].workload_level == WorkloadLevel.HIGH


def test_workload_endpoint_requires_admin(api, make_user):
    worker = api(make_user(Module.TASKS))
    resp = worker.get("/api/tasks/workload")
    assert resp.status_code == 403

    admin = api(make_user(admin=True))
    resp = admin.get("/api/tasks/workload")
    assert resp.status_code == 200
