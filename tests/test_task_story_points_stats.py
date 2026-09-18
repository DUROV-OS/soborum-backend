"""app.tasks.story_points_stats (0070-d): подсчёт сторипоинтов по сотруднику
для скрытой ИИ-подсистемы - completed по завершённым задачам за период,
open по текущим открытым. story_points не оценивается в тестах реальным ИИ
(ANTHROPIC_API_KEY не задан в тестовом окружении) - проставляется напрямую
на модели, как если бы оценку уже сделал app.ai.story_points.
"""

from datetime import datetime, timedelta, timezone

from app.common.module_access import Module
from app.tasks import service as task_service
from app.tasks import timelog
from app.tasks.models import TaskStageEvent, TaskStatus
from app.tasks.story_points_stats import completed_points_by_user, open_points_by_user


def _finish(db, task, worker):
    """Провести задачу без проверяющего до DONE (auto-close)."""
    task_service.set_status(db, task, TaskStatus.IN_PROGRESS, worker)
    task_service.set_status(db, task, TaskStatus.IN_REVIEW, worker)
    db.flush()


def _backdate_completion(db, task_id, completed_at):
    """Сдвинуть весь журнал событий задачи так, чтобы её завершение пришлось
    на `completed_at`, сохранив относительный порядок событий (иначе
    `_recompute` пересортирует их и сломает вычисление)."""
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
    task = db.get(task_service.Task, task_id)
    timelog._recompute(db, task)


def test_completed_points_by_user_sums_only_done_tasks_since(db, make_user):
    worker = make_user(Module.TASKS)

    done_recent = task_service.create_task(db, title="Задача 1", assignee_ids=[worker.id])
    done_recent.story_points = 5
    _finish(db, done_recent, worker)

    done_old = task_service.create_task(db, title="Задача 2", assignee_ids=[worker.id])
    done_old.story_points = 3
    _finish(db, done_old, worker)
    _backdate_completion(db, done_old.id, datetime.now(timezone.utc) - timedelta(days=30))

    open_task = task_service.create_task(db, title="Задача 3", assignee_ids=[worker.id])
    open_task.story_points = 8
    db.commit()

    since = datetime.now(timezone.utc) - timedelta(days=1)
    result = completed_points_by_user(db, since)
    assert result == {worker.id: 5}  # только done_recent - done_old завершена раньше since, open_task не done

    since_all_time = datetime.now(timezone.utc) - timedelta(days=60)
    result_all = completed_points_by_user(db, since_all_time)
    assert result_all == {worker.id: 8}  # done_recent (5) + done_old (3)


def test_completed_points_treats_null_story_points_as_zero(db, make_user):
    worker = make_user(Module.TASKS)
    task = task_service.create_task(db, title="Без оценки", assignee_ids=[worker.id])
    assert task.story_points is None
    _finish(db, task, worker)
    db.commit()

    since = datetime.now(timezone.utc) - timedelta(days=1)
    result = completed_points_by_user(db, since)
    assert result.get(worker.id, 0) == 0


def test_open_points_by_user_sums_only_open_tasks(db, make_user):
    worker = make_user(Module.TASKS)

    open_task = task_service.create_task(db, title="Открытая", assignee_ids=[worker.id])
    open_task.story_points = 5

    done_task = task_service.create_task(db, title="Закрытая", assignee_ids=[worker.id])
    done_task.story_points = 8
    _finish(db, done_task, worker)
    db.commit()

    result = open_points_by_user(db)
    assert result == {worker.id: 5}


def test_points_split_across_multiple_assignees(db, make_user):
    a = make_user(Module.TASKS)
    b = make_user(Module.TASKS)
    task = task_service.create_task(db, title="Общая задача", assignee_ids=[a.id, b.id])
    task.story_points = 5
    db.commit()

    result = open_points_by_user(db)
    # У каждого исполнителя учитывается полный объём задачи, не делится пополам.
    assert result == {a.id: 5, b.id: 5}


def test_story_points_never_leak_into_task_api(api, make_user):
    """Скрытость (0070-d, п.6): story_points существует только в БД, наружу
    через GET/POST/PATCH /api/tasks* не отдаётся никогда."""
    admin = api(make_user(admin=True))
    resp = admin.post("/api/tasks/", json={"title": "Задача", "assignee_ids": [], "reviewer_ids": []})
    assert resp.status_code == 201
    assert "story_points" not in resp.json()

    task_id = resp.json()["id"]
    resp = admin.get(f"/api/tasks/{task_id}")
    assert "story_points" not in resp.json()

    resp = admin.get("/api/tasks/", params={"scope": "all"})
    assert all("story_points" not in t for t in resp.json())
