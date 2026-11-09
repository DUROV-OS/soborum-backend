from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import pytest
from app.ai import analytics, cache
from app.ai.provenance import fingerprint
from app.common.module_access import Module
from app.core.config import settings
from app.core import llm
from app.agents import legal
from app.agents.models import AgentShift, AgentShiftItem
from app.production import deadlines
from app.tasks.models import Task, TaskStatus
from tests.test_production_deadlines import _make_production, _make_block


def model(monkeypatch, payload):
    calls=[]
    monkeypatch.setattr(settings,'anthropic_api_key','test-only')
    def create(**kwargs):
        calls.append(kwargs)
        data=payload() if callable(payload) else payload
        return SimpleNamespace(content=[SimpleNamespace(type='tool_use',name=analytics.SUBMIT_TOOL_NAME,input=data)])
    monkeypatch.setattr(llm,'llm_client',lambda **kwargs:SimpleNamespace(messages=SimpleNamespace(create=create)))
    return calls


def snapshot(monkeypatch):
    data={'total_materials':10,'materials_needing_supply':1}
    monkeypatch.setitem(analytics.SECTION_BUILDERS,'warehouse',(Module.WAREHOUSE,lambda db:dict(data)))
    return data


def test_changed_input_invalidates_cached_ai_and_retains_exact_source(db,make_user,monkeypatch):
    data=snapshot(monkeypatch)
    calls=model(monkeypatch,{'summary':'Нужна сверка склада.','status':'green','citations':['materials_needing_supply']})
    user=make_user(Module.WAREHOUSE)
    first=analytics.generate_section_analytics(db,user,'warehouse')
    assert first.source=='ai' and first.status=='yellow' and first.human_approved is False
    assert first.snapshot==data and first.source_hash==fingerprint({'section':'warehouse','snapshot':data})
    second=analytics.generate_section_analytics(db,user,'warehouse')
    assert second.cached and second.generated_at==first.generated_at and len(calls)==1
    data['materials_needing_supply']=5
    third=analytics.generate_section_analytics(db,user,'warehouse')
    assert third.source_hash!=first.source_hash and len(calls)==2 and not third.cached
    assert first.snapshot['materials_needing_supply']==1


@pytest.mark.parametrize('payload',[
    None, [], {'summary':'Всё готово','status':'green'},
    {'summary':'Всё готово','status':'green','citations':['invented_document']},
    {'summary':17,'status':'green','citations':['total_materials']},
    {'summary':'Всё готово','status':'invalid','citations':['total_materials']},
])
def test_invalid_or_unsourced_model_output_is_not_published(db,make_user,monkeypatch,payload):
    snapshot(monkeypatch);model(monkeypatch,payload)
    result=analytics.generate_section_analytics(db,make_user(Module.WAREHOUSE),'warehouse')
    assert result.source=='rules' and result.status=='yellow' and 'Всё готово' not in result.summary
    assert result.source_hash and result.rule_findings


def test_input_changing_during_model_call_is_marked_and_not_cached(db,make_user,monkeypatch):
    data=snapshot(monkeypatch)
    def payload():
        data['materials_needing_supply']=7
        return {'summary':'Нужна сверка.','status':'yellow','citations':['total_materials']}
    model(monkeypatch,payload)
    result=analytics.generate_section_analytics(db,make_user(Module.WAREHOUSE),'warehouse')
    assert result.input_changed and result.status=='unknown'
    assert result.snapshot['materials_needing_supply']==1
    assert cache.get(db,'section_analytics:warehouse') is None


def test_legacy_cache_cannot_claim_verified_current_sources(db,make_user,monkeypatch):
    snapshot(monkeypatch)
    cache.set(db,'section_analytics:warehouse',{'section':'warehouse','summary':'Старый зелёный вывод','status':'green','generated_at':datetime.now(timezone.utc).isoformat()},datetime.now(timezone.utc))
    result=analytics.generate_section_analytics(db,make_user(Module.WAREHOUSE),'warehouse')
    assert result.source=='rules' and result.status=='yellow' and result.source_hash


def test_task_evidence_is_scoped_and_caches_do_not_cross_users(db,make_user):
    first=make_user(Module.TASKS);second=make_user(Module.TASKS)
    db.add(Task(title='Личная задача',status=TaskStatus.READY,assignees=[first],deadline=datetime.now(timezone.utc)-timedelta(days=1)))
    db.commit()
    a=analytics.generate_section_analytics(db,first,'tasks')
    b=analytics.generate_section_analytics(db,second,'tasks')
    assert a.snapshot['total_tasks']==1 and b.snapshot['total_tasks']==0
    assert a.source_hash!=b.source_hash


def test_section_requires_its_module_access(api,make_user):
    assert api(make_user(Module.AI)).get('/api/ai/warehouse/analytics').status_code==403


def test_rule_filter_never_claims_human_approval(api,make_user):
    assert 'не юридическое заключение' in legal.scan('Нейтральный текст').passport
    assert 'Юрист:' not in legal.lawyer_reply(legal.scan('Договор'))
    result=api(make_user(admin=True)).post('/api/agents/shifts').json()
    rules=[r for i in result['items'] for r in i['reviews'] if r['method']=='rules']
    assert rules and result['charts_as_of'] and result['charts_source_hash']
    for r in rules:
        assert r['human_approved'] is False and r['input_hash']==fingerprint(r['input_text'])
        assert r['rules_version']==legal.RULES_VERSION
        assert 'Юрист черновик посмотрел' not in r['text']


def test_legacy_and_tampered_review_never_receive_new_evidence(api,make_user,db):
    user=make_user(admin=True)
    shift=AgentShift(verdict='allow',summary='Исторический результат',claude_used=False)
    db.add(shift);db.flush()
    db.add(AgentShiftItem(shift_id=shift.id,agent_id='sales',daily_question='Тест',stance='Архив',citations=[],legal_verdict='allow',reviews=[
        {'reviewer':'lawyer','status':'checked_ok','text':'Юрист проверил','escalate':False,'kind':'legal'},
        {'reviewer':'lawyer','status':'checked_ok','text':'Фильтр','escalate':False,'kind':'legal','method':'rules','input_text':'изменено','input_hash':'0'*64,'rules_version':'v1'},
    ]));db.commit()
    reviews=api(user).get('/api/agents/shifts/latest').json()['items'][0]['reviews']
    assert all(r['method']=='legacy' and r['human_approved'] is False for r in reviews)


def test_deadline_cache_tracks_task_changes_and_has_source_ids(db,monkeypatch):
    production=_make_production(db);block=_make_block(db,production,requires_materials=False)
    task=Task(title='Работа',block_id=block.id,status=TaskStatus.READY,deadline=datetime.now(timezone.utc)-timedelta(days=1))
    db.add(task);db.commit()
    calls=[]
    def pick(signals):
        calls.append(signals)
        return {'title':'Проверить задачу','description':'Есть просрочка.','impact':'Нужно проверить зависимости.','signal_index':0}
    monkeypatch.setattr(deadlines,'_ai_pick_bottleneck',pick)
    first=deadlines.generate_deadline_insight(db,production)
    assert first.input_snapshot['signals'][0]['source_id']==task.id and first.human_approved is False
    assert deadlines.generate_deadline_insight(db,production).cached and len(calls)==1
    task.title='Другая работа';db.commit()
    second=deadlines.generate_deadline_insight(db,production)
    assert second.source_hash!=first.source_hash and len(calls)==2
    task.status=TaskStatus.DONE;db.commit()
    third=deadlines.generate_deadline_insight(db,production)
    assert third.source=='none' and third.title!='По графику'
