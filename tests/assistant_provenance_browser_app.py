"""Disposable acceptance fixture. No provider or company services are called."""
import tests.costing_browser_app as t  # Enforces COSTING_BROWSER_TEST=1 before setup.
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from sqlalchemy.orm import Session

from app.ai import engine, daily_plan, priorities
from app.ai.models import Chat, ChatDomain, ChatMode, Message
from app.ai.router import app as ai_app
from app.tasks.router import app as tasks_app
from app.tasks import service
from app.tasks.models import Task
from app.users.models import User
from app.core.config import settings

for path, app in [('/api/ai', ai_app), ('/api/tasks', tasks_app)]:
    app.dependency_overrides.update(t.app.dependency_overrides)
    t.app.mount(path, app)
settings.anthropic_api_key = 'synthetic-provider-never-used'
settings.ai_provider = 'anthropic'
settings.mcp_server_url = ''
settings.web_tools_enabled = False
engine.SessionLocal = lambda: Session(t.engine)


def block(data):
    return SimpleNamespace(**data, model_dump=lambda **_: data)


def fake_model(db, system, history, tools, mode, user):
    last = history[-1]['content']
    result = next((b for b in last if b.get('type') == 'tool_result'), None)
    if result:
        source = json.loads(result['content'])
        text = 'По карточке задача открыта. [' + source['source']['id'] + ']'
        return SimpleNamespace(stop_reason='end_turn', content=[block({'type': 'text', 'text': text})])
    return SimpleNamespace(stop_reason='tool_use', content=[block({'type': 'tool_use', 'id': 'synthetic-read',
                        'name': 'get_task', 'input': {'task_id': ids['task']}})])


class FakeStream:
    def __init__(self, *args, **kwargs): self.final = fake_model(*args, **kwargs)
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def __iter__(self):
        for b in self.final.content:
            yield SimpleNamespace(type='content_block_start', content_block=b)
            if b.type == 'text':
                yield SimpleNamespace(type='content_block_delta', delta=SimpleNamespace(type='text_delta', text=b.text))
            yield SimpleNamespace(type='content_block_stop')
    def get_final_message(self): return self.final


class Planner:
    def create(self, **kwargs):
        tasks = json.loads(kwargs['messages'][0]['content'])['tasks']
        name = kwargs['tool_choice']['name']
        key = 'plan' if name == daily_plan.SUBMIT_TOOL_NAME else 'priorities'
        return SimpleNamespace(stop_reason='tool_use', content=[SimpleNamespace(type='tool_use', name=name,
            input={key: [{'task_id': p['id'], 'reason': 'Выдуманное согласование руководителя'} for p in tasks[:3]]})])


engine._call_claude = fake_model
engine._stream_claude = FakeStream
daily_plan._get_client = priorities._get_client = lambda: SimpleNamespace(messages=Planner())

t.reset()
with Session(t.engine) as db:
    owner = db.get(User, 1)
    task = service.create_task(db, title='Проверить подтверждающий документ', assignee_ids=[owner.id])
    task.deadline = datetime.now(timezone.utc) - timedelta(days=1)
    db.commit()
    ids = {'synthetic': True, 'task': task.id}
    chat = Chat(owner_id=owner.id, domain=ChatDomain.GENERAL, mode=ChatMode.NO_ACTIONS, title='Проверяемый ответ')
    db.add(chat); db.commit()
    engine.run_turn(db, chat, owner, 'На каком основании составлен ответ?')
    ids['chat'] = chat.id
    unknown = Chat(owner_id=owner.id, domain=ChatDomain.GENERAL, mode=ChatMode.NO_ACTIONS, title='Ответ без источников')
    db.add(unknown); db.commit()
    msg = engine._persist_assistant(db, unknown, [{'type': 'text', 'text': 'Нет данных для расчёта. Ссылка [S999-1] не подтверждена.'}])
    from app.ai import answer_provenance
    answer_provenance.record_answer(db, unknown, msg, owner, system='synthetic', stop_reason='end_turn')
    ids['unknown_chat'] = unknown.id
    legacy = Chat(owner_id=owner.id, domain=ChatDomain.GENERAL, mode=ChatMode.NO_ACTIONS, title='Исторический ответ')
    db.add(legacy); db.flush()
    db.add(Message(chat_id=legacy.id, role='assistant', content=[{'type':'text','text':'Старый ответ без снимка.'}]))
    db.commit(); ids['legacy_chat'] = legacy.id


@t.app.get('/api/assistant-test/data')
def data(): return ids


@t.app.post('/api/assistant-test/change-deadline')
def change_deadline():
    with Session(t.engine) as db:
        task = db.get(Task, ids['task'])
        task.deadline = datetime.now(timezone.utc) + timedelta(days=3)
        db.commit()
    return {'synthetic': True}


@t.app.get('/api/feedback/my')
def feedback(): return []


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(t.app, host='0.0.0.0', port=8133, log_level='warning')
