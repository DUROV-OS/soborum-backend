"""app.tasks.story_points_backfill (0070-d): создание задачи не ждёт ИИ,
очки проставляет фоновая порция — новые задачи первыми, в пределах лимита,
неоценённые остаются в очереди. Реальный ИИ заменён подменой
app.ai.story_points.estimate_story_points."""

import pytest

from app.ai import story_points as ai_story_points
from app.common.module_access import Module
from app.tasks import service as task_service
from app.tasks import story_points_backfill
from app.tasks.models import Task, TaskLinkType


@pytest.fixture
def llm_on(monkeypatch):
    monkeypatch.setattr(story_points_backfill.settings, "ai_provider", "openai")
    monkeypatch.setattr(story_points_backfill.settings, "openai_api_key", "fake-key-for-tests")


def test_creating_tasks_never_calls_ai(db, make_user, monkeypatch, llm_on):
    monkeypatch.setattr(
        ai_story_points, "estimate_story_points", lambda *a: pytest.fail("ИИ вызван при создании задачи")
    )
    worker = make_user(Module.TASKS)

    manual = task_service.create_task(db, title="Ручная", assignee_ids=[worker.id])
    linked = task_service.create_link_task(
        db, title="Связанная", link_type=TaskLinkType.CLIENT_STAGE, link_id=1, assignees=[worker]
    )
    db.commit()

    assert manual.story_points is None
    assert linked.story_points is None


def test_estimate_pending_newest_first_within_limit(db, make_user, monkeypatch, llm_on):
    calls: list[str] = []

    def fake(title, description, section_hint):
        calls.append(title)
        return 3

    monkeypatch.setattr(ai_story_points, "estimate_story_points", fake)
    worker = make_user(Module.TASKS)
    tasks = [task_service.create_task(db, title=f"Задача {i}", assignee_ids=[worker.id]) for i in range(5)]
    db.commit()

    assert story_points_backfill.estimate_pending(db, limit=2) == (2, 2)
    assert calls == ["Задача 4", "Задача 3"]
    db.expire_all()
    assert [db.get(Task, t.id).story_points for t in tasks] == [None, None, None, 3, 3]


def test_unestimated_tasks_stay_queued(db, make_user, monkeypatch, llm_on):
    monkeypatch.setattr(
        ai_story_points, "estimate_story_points", lambda title, *_: None if title == "Сбойная" else 5
    )
    worker = make_user(Module.TASKS)
    ok = task_service.create_task(db, title="Нормальная", assignee_ids=[worker.id])
    bad = task_service.create_task(db, title="Сбойная", assignee_ids=[worker.id])
    db.commit()

    assert story_points_backfill.backfill_missing(db) == 1
    db.expire_all()
    assert db.get(Task, ok.id).story_points == 5
    assert db.get(Task, bad.id).story_points is None


def test_estimate_pending_without_provider_key_does_nothing(db, make_user, monkeypatch):
    monkeypatch.setattr(story_points_backfill.settings, "ai_provider", "openai")
    monkeypatch.setattr(story_points_backfill.settings, "openai_api_key", "")
    monkeypatch.setattr(ai_story_points, "estimate_story_points", lambda *a: pytest.fail("без ключа ИИ не вызывается"))
    worker = make_user(Module.TASKS)
    task_service.create_task(db, title="Задача", assignee_ids=[worker.id])
    db.commit()

    assert story_points_backfill.estimate_pending(db, limit=10) == (0, 0)
