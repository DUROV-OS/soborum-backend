from datetime import datetime, timezone
import pytest
from app.common.module_access import Module
from app.crm.models import CRMEntity, CRMEvent
from app.crm.work_queue import personal_queue


NOW = datetime(2026, 11, 10, 20, 30, tzinfo=timezone.utc)  # 23:30 in Moscow


def row(db, kind, name, owner=None, **payload):
    value = CRMEntity(kind=kind, name=name, payload={**payload, 'responsible_user_id': owner.id if owner else None})
    db.add(value); db.flush()
    return value


def test_personal_queue_uses_moscow_day_and_real_task_status(db, make_user):
    user, other = make_user(Module.CLIENTS), make_user(Module.CLIENTS)
    row(db, 'task', 'Просроченная', user, due_at='2026-11-10T20:29:00Z')
    row(db, 'task', 'Сегодня', user, due_at='2026-11-10T23:50:00+03:00')
    row(db, 'task', 'Завтра в Москве', user, due_at='2026-11-10T21:30:00Z')
    row(db, 'task', 'Без срока', user)
    row(db, 'task', 'Некорректный старый срок', user, due_at='not-a-date')
    row(db, 'task', 'Чужая', other, due_at='2026-11-09T00:00:00Z')
    row(db, 'task', 'Завершённая', user, completed=True, due_at='2026-11-09T00:00:00Z')
    archived = row(db, 'task', 'Архивная', user); archived.archived = True
    db.commit()
    result = personal_queue(db, user, now=NOW)
    assert result['counts'] == {'overdue': 1, 'today': 1, 'upcoming': 1, 'no_deadline': 2, 'no_next_step': 0}
    assert result['total'] == 5 and result['owner_id'] == user.id
    assert result['items'][0]['title'] == 'Просроченная'
    assert next(i for i in result['items'] if i['title'] == 'Некорректный старый срок')['deadline_invalid']
    assert next(i for i in result['items'] if i['title'] == 'Сегодня')['due_at'] == '2026-11-10T20:50:00+00:00'


def test_own_deals_need_an_open_step_even_if_another_colleague_executes_it(db, make_user):
    user, other = make_user(Module.CLIENTS), make_user(Module.CLIENTS)
    open_stage = row(db, 'stage', 'Открыто', outcome='open')
    won = row(db, 'stage', 'Выиграно', outcome='won')
    missing = row(db, 'deal', 'Назначить следующий шаг', user, stage_id=open_stage.id)
    assigned = row(db, 'deal', 'Работает коллега', user, stage_id=open_stage.id)
    row(db, 'task', 'Звонок коллеги', other, entity_id=assigned.id)
    row(db, 'task', 'Предыдущий звонок', user, entity_id=missing.id, completed=True)
    row(db, 'deal', 'Закрытая', user, stage_id=won.id)
    row(db, 'deal', 'Входящая', user, is_unsorted=True)
    row(db, 'deal', 'Чужая', other)
    archived = row(db, 'deal', 'Архивная', user); archived.archived = True
    db.commit()
    result = personal_queue(db, user, now=NOW)
    assert result['total'] == 1
    assert result['items'][0]['id'] == missing.id
    assert result['items'][0]['bucket'] == 'no_next_step'


def test_filter_counts_and_pagination_are_consistent(db, make_user):
    user = make_user(Module.CLIENTS)
    ids = [row(db, 'task', f'Задача {i}', user).id for i in range(23)]
    row(db, 'task', 'Позже', user, due_at='2026-12-01T00:00:00Z')
    db.commit()
    result = personal_queue(db, user, now=NOW, bucket='no_deadline', offset=10, limit=10)
    assert result['total'] == 23 and result['counts']['upcoming'] == 1
    assert [i['id'] for i in result['items']] == ids[10:20]
    assert personal_queue(db, user, now=NOW, bucket='today')['items'] == []


def test_spam_is_hidden_but_a_real_assignment_on_an_archived_card_remains(db, make_user):
    user = make_user(Module.CLIENTS)
    spam = row(db, 'inquiry', 'Спам', is_spam=True)
    archived = row(db, 'deal', 'Реальная карточка'); archived.archived = True
    row(db, 'task', 'Игнорировать спам', user, entity_id=spam.id)
    task = row(db, 'task', 'Проверить архивную карточку', user, entity_id=archived.id)
    db.commit()
    result = personal_queue(db, user, now=NOW)
    assert [i['id'] for i in result['items']] == [task.id]
    assert result['items'][0]['related']['archived'] is True


def test_hidden_spam_deal_does_not_reappear_as_a_task(db, make_user):
    user = make_user(Module.CLIENTS)
    hidden = row(db, 'deal', 'Скрытая спам-сделка', is_unsorted=True)
    row(db, 'inquiry', 'Спам', deal_id=hidden.id, is_spam=True)
    task = row(db, 'task', 'Задача скрытой сделки', user, entity_id=hidden.id)
    db.commit()
    assert personal_queue(db, user, now=NOW)['total'] == 0
    row(db, 'inquiry', 'Реальное обращение', deal_id=hidden.id, is_spam=False)
    db.commit()
    assert personal_queue(db, user, now=NOW)['items'][0]['id'] == task.id


def test_api_cannot_change_queue_owner_and_get_never_writes(db, make_user, api):
    user, other = make_user(Module.CLIENTS), make_user(Module.CLIENTS)
    own = row(db, 'task', 'Моя задача', user)
    row(db, 'task', 'Чужой секретный заголовок', other)
    db.commit()
    before = (db.query(CRMEntity).count(), db.query(CRMEvent).count(), own.version)
    response = api(user).get(f'/api/crm/my-work?owner_id={other.id}')
    assert response.status_code == 200
    result = response.json()
    assert result['owner_id'] == user.id and [i['id'] for i in result['items']] == [own.id]
    assert (db.query(CRMEntity).count(), db.query(CRMEvent).count(), own.version) == before
    assert api(make_user(Module.WAREHOUSE)).get('/api/crm/my-work').status_code == 403
    assert api(user).get('/api/crm/my-work?bucket=unknown').status_code == 422
    assert api(user).get('/api/crm/my-work?offset=-1').status_code == 422


def test_completion_is_reflected_without_saved_dashboard_counters(db, make_user, api):
    from app.crm import service
    from app.crm.schemas import Entry, Patch
    user = make_user(admin=True)
    task = service.create(db, 'task', Entry(name='Проверить договор', data={'responsible_user_id': user.id}), user)
    assert api(user).get('/api/crm/my-work').json()['total'] == 1
    service.patch(db, task['id'], Patch(version=task['version'], data={'completed': True, 'result': 'Договор проверен'}), user)
    assert api(user).get('/api/crm/my-work').json()['total'] == 0


def test_card_task_filter_is_applied_before_count_and_pagination(db, make_user, api):
    user = make_user(Module.CLIENTS)
    parent = row(db, 'contact', 'Клиент')
    other = row(db, 'contact', 'Другой клиент')
    wanted = [row(db, 'task', f'Наша {i}', user, entity_id=parent.id).id for i in range(3)]
    for i in range(110):row(db, 'task', f'Чужая {i}', user, entity_id=other.id)
    db.commit()
    result = api(user).get(f'/api/crm/entries?kind=task&entity_id={parent.id}&limit=2').json()
    assert result['total'] == 3 and [t['id'] for t in result['items']] == wanted[::-1][:2]
    assert api(user).get(f'/api/crm/entries?kind=deal&entity_id={parent.id}').status_code == 422
    assert api(user).get('/api/crm/entries?kind=task&entity_id=999999').status_code == 404


def test_history_identifies_the_actual_author_without_contact_credentials(db, make_user, api):
    owner = make_user(admin=True)
    viewer = make_user(Module.CLIENTS)
    card = row(db, 'contact', 'Клиент')
    event = CRMEvent(entity_id=card.id, actor_id=owner.id, action='note', details={'text': 'Договорились о встрече'})
    db.add(event); db.commit()
    result = api(viewer).get(f'/api/crm/entries/{card.id}/history').json()['items'][0]
    assert result['actor'] == {'id': owner.id, 'name': owner.full_name}
    assert result['action'] == 'note' and result['created_at'].endswith('+00:00')
    assert 'email' not in str(result) and owner.email not in str(result)


@pytest.mark.parametrize('owner_is_admin', [False, True])
def test_admin_also_gets_own_queue_not_colleagues_work(db, make_user, api, owner_is_admin):
    user = make_user(admin=owner_is_admin) if owner_is_admin else make_user(Module.CLIENTS)
    other = make_user(Module.CLIENTS)
    row(db, 'task', 'Другой исполнитель', other)
    db.commit()
    assert api(user).get('/api/crm/my-work').json()['total'] == 0
