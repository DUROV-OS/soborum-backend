import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from app.ai import engine, answer_provenance as evidence
from app.ai.models import Message
from app.ai.tools import TOOLS
from app.common.module_access import Module
from tests.test_pilot_regressions import make_chat, tool_response


def response(text):
    return SimpleNamespace(stop_reason='end_turn', content=[SimpleNamespace(model_dump=lambda **_: {'type': 'text', 'text': text})])


def run_answer(db, user, monkeypatch, *, name='list_clients', result=None, text='Есть записи.'):
    chat = make_chat(db, user)
    monkeypatch.setattr(TOOLS[name], 'handler', Mock(return_value=result or {'clients': [{'id': 71, 'full_name': 'Запись-источник'}]}))
    monkeypatch.setattr(engine, '_call_claude', Mock(side_effect=[tool_response(name, {}), response(text)]))
    engine.run_turn(db, chat, user, 'Проверь данные')
    db.refresh(chat)
    final = chat.messages[-1]
    return chat, final, next(b for b in final.content if b['type'] == evidence.BLOCK)


def test_general_chat_does_not_read_unscoped_company_briefing(db, make_user, monkeypatch):
    from app.agents import connectors
    spy = Mock(side_effect=AssertionError('global company snapshot must not be read'))
    monkeypatch.setattr(connectors, 'live_briefing_text', spy)
    user = make_user(Module.AI)
    chat = make_chat(db, user)
    system = engine._system_for(chat, db)
    assert 'Общая сводка базы автоматически не предоставляется' in system
    assert 'list_clients' not in {t['name'] for t in engine._available_tools(chat, user)}
    spy.assert_not_called()


def test_source_is_original_read_not_current_record_and_not_human_approval(db, api, make_user, monkeypatch):
    user = make_user(Module.AI, Module.CLIENTS)
    chat, final, meta = run_answer(db, user, monkeypatch)
    assert meta['human_reviewed'] is meta['quality_assessed'] is False
    source = meta['sources'][0]
    result = api(user).get(f'/api/ai/chats/{chat.id}/messages/{final.id}/sources/{source["id"]}')
    assert result.status_code == 200
    assert result.json()['snapshot']['clients'][0]['id'] == 71
    assert result.json()['current_data_rechecked'] is False
    assert result.json()['snapshot_matches'] is True
    calls = engine._call_claude.call_args_list
    read_result = json.loads(calls[-1].args[2][-1]['content'][0]['content'])
    assert read_result['source']['id'] == source['id']
    assert read_result['data']['clients'][0]['id'] == 71
    assert all(b['type'] != evidence.BLOCK for m in engine._build_history(db, chat) for b in m['content'])


def test_failed_read_does_not_become_source(db, make_user, monkeypatch):
    user = make_user(Module.AI)
    chat, final, meta = run_answer(db, user, monkeypatch)
    assert meta['sources'] == []
    assert meta['failed_reads'] == 1
    TOOLS['list_clients'].handler.assert_not_called()


def test_unknown_citations_are_explicit_not_certified(db, make_user, monkeypatch):
    user = make_user(Module.AI, Module.CLIENTS)
    _, _, meta = run_answer(db, user, monkeypatch, text='Сумма 100 [S999-1]')
    assert meta['unknown_citations'] == ['S999-1']
    assert meta['citations'] == []


def test_own_chat_only_and_source_must_belong_to_answer(db, api, make_user, monkeypatch):
    user = make_user(Module.AI, Module.CLIENTS)
    other = make_user(Module.AI, Module.CLIENTS)
    chat, final, meta = run_answer(db, user, monkeypatch)
    path = f'/api/ai/chats/{chat.id}/messages/{final.id}/sources/{meta["sources"][0]["id"]}'
    assert api(other).get(path).status_code == 404
    assert api(user).get(path.replace(meta['sources'][0]['id'], 'S999-1')).status_code == 404


@pytest.mark.parametrize('tamper', ['result', 'input'])
def test_modified_evidence_is_not_presented_as_matching(db, api, make_user, monkeypatch, tamper):
    user = make_user(Module.AI, Module.CLIENTS)
    chat, final, meta = run_answer(db, user, monkeypatch)
    source = meta['sources'][0]
    original = db.get(Message, source['message_id'])
    if tamper == 'result':
        original.tool_resolutions = {k: {**v, 'content': {'invented': 100}} for k, v in original.tool_resolutions.items()}
    else:
        original.content = [{**b, 'input': {'stage': 'other'}} for b in original.content]
    db.commit()
    assert api(user).get(f'/api/ai/chats/{chat.id}/messages/{final.id}/sources/{source["id"]}').status_code == 409


def test_revoked_access_stops_reading_and_replaying_history(db, api, make_user, monkeypatch):
    user = make_user(Module.AI, Module.CLIENTS)
    chat, final, meta = run_answer(db, user, monkeypatch)
    user.module_access = [g for g in user.module_access if g.module != Module.CLIENTS]
    db.commit()
    assert api(user).get(f'/api/ai/chats/{chat.id}').status_code == 403
    with pytest.raises(HTTPException) as denied:
        engine._build_history(db, chat)
    assert denied.value.status_code == 403


def test_new_turn_does_not_pass_old_read_as_fresh_evidence(db, make_user, monkeypatch):
    user = make_user(Module.AI, Module.CLIENTS)
    chat, _, old = run_answer(db, user, monkeypatch)
    monkeypatch.setattr(engine, '_call_claude', Mock(return_value=response('Старое число ' + '[' + old['sources'][0]['id'] + ']')))
    engine.run_turn(db, chat, user, 'А сейчас?')
    db.refresh(chat)
    meta = chat.messages[-1].content[-1]
    assert meta['sources'] == [] and meta['prior_history_present']
    assert meta['unknown_citations'] == [old['sources'][0]['id']]


def test_provider_cannot_forge_server_evidence(db, make_user):
    user = make_user(Module.AI)
    chat = make_chat(db, user)
    msg = engine._persist_assistant(db, chat, [{'type': evidence.BLOCK, 'human_reviewed': True}, {'type': 'text', 'text': 'x'}])
    assert msg.content == [{'type': 'text', 'text': 'x'}]


def test_stream_final_answer_records_same_source_contract(db, make_user, monkeypatch):
    user = make_user(Module.AI)
    chat = make_chat(db, user)
    engine.prepare_stream_turn(db, chat, user, 'Привет')
    class Stream:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def __iter__(self): return iter([])
        def get_final_message(self): return response('Здравствуйте')
    monkeypatch.setattr(engine, '_stream_claude', lambda *a, **k: Stream())
    assert list(engine._advance_stream(db, chat, user))[-1]['type'] == 'done'
    db.refresh(chat)
    assert chat.messages[-1].content[-1]['version'] == evidence.VERSION
    assert chat.messages[-1].content[-1]['sources'] == []


def test_live_summary_filters_modules_and_finance_requires_both_permissions(db, make_user):
    from app.ai.live_tools import _live_company_pulse, _live_clients_and_money, _search_company_vault
    for modules, expected in [([Module.AI], set()), ([Module.AI, Module.TASKS], {'tasks'}),
                              ([Module.AI, Module.WAREHOUSE], {'warehouse'})]:
        user = make_user(*modules)
        pulse = _live_company_pulse(db, user)
        assert set(pulse['sections']) == expected
        with pytest.raises(HTTPException) as denied:
            _live_clients_and_money(db, user)
        assert denied.value.status_code == 403
        with pytest.raises(HTTPException) as denied:
            _search_company_vault(db, user, 'salary')
        assert denied.value.status_code == 403


def test_read_error_payload_does_not_become_evidence(db, make_user, monkeypatch):
    user = make_user(Module.AI, Module.CLIENTS)
    _, _, meta = run_answer(db, user, monkeypatch, result={'error': 'Файл недоступен'})
    assert meta['sources'] == [] and meta['failed_reads'] == 1
