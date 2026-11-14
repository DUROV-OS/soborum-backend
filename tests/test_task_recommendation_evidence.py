from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.ai import daily_plan, priorities
from app.ai import task_recommendations as recommendations
from app.common.module_access import Module
from app.tasks import service
from app.tasks.models import TaskStatus


def configure(monkeypatch, module, rows, callback=None):
    key = 'plan' if module is daily_plan else 'priorities'
    def create(**kwargs):
        if callback: callback()
        return SimpleNamespace(stop_reason='tool_use', content=[SimpleNamespace(type='tool_use', name=module.SUBMIT_TOOL_NAME, input={key: rows})])
    client = SimpleNamespace(messages=SimpleNamespace(create=Mock(side_effect=create)))
    monkeypatch.setattr(module, '_get_client', lambda: client)
    return client.messages.create


@pytest.mark.parametrize('module', [daily_plan, priorities])
def test_changed_task_invalidates_cache_and_facts_replace_model_claims(db, make_user, monkeypatch, module):
    user = make_user(Module.AI, Module.TASKS)
    task = service.create_task(db, title='Проверка', assignee_ids=[user.id]); db.commit()
    call = configure(monkeypatch, module, [{'task_id': task.id, 'reason': 'Руководитель согласовал прибыль 99%'}])
    run = module.generate_daily_plan if module is daily_plan else module.generate_task_priorities
    one = run(db, user); two = run(db, user)
    assert two.cached and call.call_count == 1
    assert '99%' not in one.model_dump_json() and 'согласовал' not in one.model_dump_json()
    task.deadline = datetime.now(timezone.utc) - timedelta(days=1); db.commit()
    three = run(db, user)
    assert not three.cached and call.call_count == 2
    assert three.source_hash != one.source_hash
    assert 'Срок задачи уже прошёл' in three.model_dump_json()
    assert three.quality_assessed is False


@pytest.mark.parametrize('bad', [None, {}, 'bad', [{'task_id': True}], [{'task_id': 999999}], [{'task_id': 1}, {'task_id': 1}]])
def test_invalid_model_selection_falls_back_without_false_empty_state(db, make_user, monkeypatch, bad):
    user = make_user(Module.AI, Module.TASKS)
    task = service.create_task(db, title='Нужная задача', assignee_ids=[user.id]); db.commit()
    configure(monkeypatch, daily_plan, bad)
    report = daily_plan.generate_daily_plan(db, user)
    assert report.source == 'rules' and report.fallback_reason
    assert [i.task.id for i in report.plan] == [task.id]


def test_task_finished_during_generation_not_returned_or_cached_as_ai(db, make_user, monkeypatch):
    user = make_user(Module.AI, Module.TASKS)
    task = service.create_task(db, title='Завершается', assignee_ids=[user.id]); db.commit()
    def finish():
        task.status = TaskStatus.DONE; db.commit()
    configure(monkeypatch, daily_plan, [{'task_id': task.id}], finish)
    report = daily_plan.generate_daily_plan(db, user)
    assert report.plan == [] and report.input_changed and report.source == 'rules'


def test_hidden_or_new_tasks_do_not_leave_stale_cached_plan(db, make_user, monkeypatch):
    user = make_user(Module.AI, Module.TASKS); other = make_user(Module.TASKS)
    task = service.create_task(db, title='Первый', assignee_ids=[user.id]); db.commit()
    configure(monkeypatch, daily_plan, [{'task_id': task.id}])
    old = daily_plan.generate_daily_plan(db, user)
    task.assignees = [other]
    urgent = service.create_task(db, title='Новый срочный', assignee_ids=[user.id]); db.commit()
    configure(monkeypatch, daily_plan, [{'task_id': urgent.id}])
    report = daily_plan.generate_daily_plan(db, user)
    assert report.source_hash != old.source_hash and not report.cached
    assert [i.task.id for i in report.plan] == [urgent.id]


def test_moscow_day_and_candidate_scope_are_explicit(db, make_user, monkeypatch):
    user = make_user(Module.AI, Module.TASKS)
    task = service.create_task(db, title='Завтра по UTC, сегодня по Москве', assignee_ids=[user.id])
    task.deadline = datetime(2026, 11, 10, 23, 30, tzinfo=timezone.utc); db.commit()
    call = configure(monkeypatch, daily_plan, [{'task_id': task.id}])
    report = recommendations.generate(db, user, daily=True, load_candidates=daily_plan._load_candidate_tasks,
        get_client=daily_plan._get_client, prompt=daily_plan.SYSTEM_PROMPT, tool_schema=daily_plan.TOOL_SCHEMA,
        now=datetime(2026, 11, 10, 22, 0, tzinfo=timezone.utc))
    assert '2026-11-11' in call.call_args.kwargs['messages'][0]['content']
    assert 'сегодня по Москве' in report.plan[0].reason
    assert report.candidate_count == report.considered_count == 1
    assert 'story_points' not in report.model_dump_json()
