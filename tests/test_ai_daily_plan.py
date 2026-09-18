"""app.ai.daily_plan (0070-e): план на день не придумывает задачи, которых
ИИ не видел (фильтрует id вне кандидатов, схлопывает дубликаты), передаёт в
промпт сторипоинты/приоритет/чужую загруженность, но не отдаёт их наружу, и
кэширует результат на день. Реальный вызов Claude заменён фейковым клиентом
- ANTHROPIC_API_KEY в тестовом окружении не задан (см. tests/conftest.py).
"""

from types import SimpleNamespace

from app.ai import daily_plan
from app.common.module_access import Module
from app.tasks import service as task_service


class _FakeToolUse:
    type = "tool_use"

    def __init__(self, input_):
        self.input = input_


class _FakeMessages:
    def __init__(self, plan_input, calls):
        self._plan_input = plan_input
        self._calls = calls

    def create(self, **kwargs):
        self._calls.append(kwargs)
        return SimpleNamespace(content=[_FakeToolUse({"plan": self._plan_input})])


class _FakeClient:
    def __init__(self, plan_input, calls):
        self.messages = _FakeMessages(plan_input, calls)


def _patch_client(monkeypatch, plan_input):
    calls: list[dict] = []
    monkeypatch.setattr(daily_plan.settings, "anthropic_api_key", "fake-key-for-tests")
    monkeypatch.setattr(daily_plan, "_get_client", lambda: _FakeClient(plan_input, calls))
    return calls


def test_daily_plan_drops_hallucinated_and_duplicate_task_ids(db, make_user, monkeypatch):
    worker = make_user(Module.TASKS)
    real = task_service.create_task(db, title="Реальная задача", assignee_ids=[worker.id])
    db.commit()

    _patch_client(
        monkeypatch,
        [
            {"task_id": real.id, "reason": "Реальная"},
            {"task_id": 999999, "reason": "Выдуманная - такой задачи нет"},
            {"task_id": real.id, "reason": "Дубликат"},
        ],
    )

    result = daily_plan.generate_daily_plan(db, worker)
    assert [item.task.id for item in result.plan] == [real.id]


def test_daily_plan_result_never_includes_story_points_or_load_numbers(db, make_user, monkeypatch):
    worker = make_user(Module.TASKS)
    task = task_service.create_task(db, title="Задача", assignee_ids=[worker.id])
    task.story_points = 5
    db.commit()

    _patch_client(monkeypatch, [{"task_id": task.id, "reason": "По приоритету"}])

    result = daily_plan.generate_daily_plan(db, worker)
    dumped = result.model_dump()
    assert "story_points" not in str(dumped)


def test_daily_plan_is_cached_within_the_same_day(db, make_user, monkeypatch):
    worker = make_user(Module.TASKS)
    task = task_service.create_task(db, title="Задача", assignee_ids=[worker.id])
    db.commit()

    calls = _patch_client(monkeypatch, [{"task_id": task.id, "reason": "Первый расчёт"}])

    first = daily_plan.generate_daily_plan(db, worker)
    second = daily_plan.generate_daily_plan(db, worker)
    assert len(calls) == 1  # второй вызов обслужен из кэша, Claude не дёргали повторно
    assert [i.task.id for i in second.plan] == [i.task.id for i in first.plan]

    third = daily_plan.generate_daily_plan(db, worker, force=True)
    assert len(calls) == 2  # reload=true обходит кэш
    assert [i.task.id for i in third.plan] == [task.id]


def test_daily_plan_empty_when_no_open_tasks(db, make_user, monkeypatch):
    worker = make_user(Module.TASKS)
    calls = _patch_client(monkeypatch, [])

    result = daily_plan.generate_daily_plan(db, worker)
    assert result.plan == []
    assert len(calls) == 0  # нет кандидатов - Claude вообще не вызывается
