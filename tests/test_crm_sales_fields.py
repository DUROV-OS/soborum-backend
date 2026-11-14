"""Only isolated synthetic records; never imports or changes live customer data."""
from copy import deepcopy
import pytest
from fastapi import HTTPException
from app.common.module_access import AccessLevel, Module
from app.crm import service
from app.crm.models import CRMEntity, CRMEvent
from app.crm.schemas import Entry, Patch
from app.crm.sales_fields import agency_sources
from app.users.models import User


def create(db, user, kind, name, **data):
    return service.create(db, kind, Entry(name=name, data=data), user)


def board(db, user, agency=False):
    pipeline = create(db, user, 'pipeline', 'Агентства' if agency else 'Продажи', purpose='agency' if agency else 'sales')
    first = create(db, user, 'stage', 'Новое агентство' if agency else 'Новые заявки', pipeline_id=pipeline['id'])
    connected = create(db, user, 'stage', 'Подключён к партнёрскому каналу', pipeline_id=pipeline['id'], agency_source_eligible=agency)
    return pipeline, first, connected


def change(db, user, row, **data):
    return service.patch(db, row['id'], Patch(version=row['version'], data=data), user)


def test_customer_profile_and_nested_patch_keep_dates_phones_and_imported_fields(db, make_user):
    user = make_user(admin=True)
    imported = [{'field_id': 14, 'field_name': 'Источник amoCRM', 'values': [{'value': 'Сайт'}]}, None]
    customer = dict(last_name='Тестов', first_name='Иван', middle_name='Иванович', gender='male',
        phone_2='+79000000002', phone_3='+79000000003', source_label='Рекомендация',
        deal_timeframe='Декабрь 2026', birth_date='1990-03-04', marital_status='Женат',
        marriage_date='2015-06-07', children_birth_dates=['2017-08-09', '2020-10-11'], city='Тамбов', temperature='hot')
    row = create(db, user, 'contact', 'Иван', phone='+79000000001', email='synthetic@example.invalid',
        custom_fields=imported, customer=customer)
    updated = change(db, user, row, customer={'city': 'Воронеж'})
    assert updated['data']['customer'] == {**customer, 'city': 'Воронеж'}
    assert updated['data']['phone'] == '+79000000001' and updated['data']['custom_fields'] == imported
    with pytest.raises(HTTPException) as conflict:
        change(db, user, row, customer={'city': 'Устаревшая правка'})
    assert conflict.value.status_code == 409


@pytest.mark.parametrize('data', [
    {'customer': {'temperature': 'urgent'}}, {'customer': {'birth_date': '2026-02-30'}},
    {'needs': {'extras': ['unknown']}}, {'needs': {'rooms': -1}},
    {'purchase_terms': {'initial_payment': -1}}, {'sale_terms': {'final_customer_price': float('inf')}},
    {'sale_terms': {'object_url': 'javascript:alert(1)'}}, {'purchase_terms': {'production_cost': 1}},
])
def test_invalid_sales_values_rejected(db, make_user, data):
    user = make_user(admin=True)
    pipeline, stage, _ = board(db, user)
    kind = 'contact' if 'customer' in data else 'deal'
    if kind == 'deal':
        data = {**data, 'pipeline_id': pipeline['id'], 'stage_id': stage['id']}
    with pytest.raises(HTTPException) as invalid:
        create(db, user, kind, 'Invalid', **data)
    assert invalid.value.status_code == 422


def test_cross_pipeline_keeps_needs_terms_primary_contact_and_history(db, make_user):
    user = make_user(admin=True)
    first, first_stage, _ = board(db, user)
    target, target_stage, _ = board(db, user)
    contact = create(db, user, 'contact', 'Клиент', customer={'temperature': 'warm'})
    deal = create(db, user, 'deal', 'Дом', pipeline_id=first['id'], stage_id=first_stage['id'],
        amount=4000000, contact_ids=[contact['id']], primary_contact_id=contact['id'],
        needs={'purchase_goal': 'living', 'construction_city': 'Тамбов', 'property_type': 'barn', 'rooms': 3,
               'finishing': 'eco', 'extras': ['sauna', 'hot_tub']},
        purchase_terms={'payment_form': 'mortgage', 'initial_payment': 1000000, 'mortgage_approved': True},
        sale_terms={'object_url': 'https://example.invalid/object/1', 'final_customer_price': 4500000,
                    'final_payment_timeframe': 'После приёмки', 'partner_deal': False, 'payment_status': 'Уточняется'})
    before = deepcopy(deal['data'])
    moved = change(db, user, deal, pipeline_id=target['id'], stage_id=target_stage['id'])
    assert moved['data'] == {**before, 'pipeline_id': target['id'], 'stage_id': target_stage['id']}
    assert moved['id'] == deal['id']
    assert service.history(db, service.get(db, deal['id']))['total'] == 2
    assert service.list_entries(db, 'deal', temperature='warm')['total'] == 1


def test_temperature_filters_before_pagination_and_budget_summaries(db, make_user, api):
    user = make_user(admin=True)
    pipeline, stage, _ = board(db, user)
    hot = create(db, user, 'contact', 'Горячий', customer={'temperature': 'hot'})
    cold = create(db, user, 'contact', 'Холодный', customer={'temperature': 'cold'})
    for index in range(105):
        db.add(CRMEntity(kind='deal', name=f'Дом {index}', payload={
            'pipeline_id': pipeline['id'], 'stage_id': stage['id'], 'amount': 100, 'contact_ids': [hot['id']]}))
    create(db, user, 'deal', 'Холодный дом', pipeline_id=pipeline['id'], stage_id=stage['id'], amount=900, contact_ids=[cold['id']])
    create(db, user, 'deal', 'Неизвестно', pipeline_id=pipeline['id'], stage_id=stage['id'], amount=400)
    viewer = api(make_user(Module.CLIENTS, level=AccessLevel.VIEW))
    page = viewer.get('/api/crm/entries', params={'kind': 'deal', 'temperature': 'hot', 'limit': 10, 'offset': 100}).json()
    assert page['total'] == 105 and len(page['items']) == 5
    summary = viewer.get('/api/crm/kanban-summary', params={'pipeline_id': pipeline['id'], 'temperature': 'hot'}).json()
    column = next(c for c in summary['columns'] if c['stage_id'] == stage['id'])
    assert column['count'] == 105 and column['amounts'] == {'RUB': 10500}
    assert viewer.get('/api/crm/entries?kind=deal&temperature=unknown').json()['total'] == 1
    assert viewer.get('/api/crm/entries?kind=deal&temperature=invalid').status_code == 422
    assert viewer.get('/api/crm/entries?kind=task&temperature=hot').status_code == 422


def test_inquiry_temperature_uses_linked_primary_contact_and_counts_once(db, make_user):
    user = make_user(admin=True)
    pipeline, stage, _ = board(db, user)
    warm = create(db, user, 'contact', 'Тёплый', customer={'temperature': 'warm'})
    cold = create(db, user, 'contact', 'Холодный', customer={'temperature': 'cold'})
    deal = create(db, user, 'deal', 'Скрытая', pipeline_id=pipeline['id'], stage_id=stage['id'], is_unsorted=True,
        amount=150, contact_ids=[cold['id'], warm['id']], primary_contact_id=warm['id'])
    create(db, user, 'inquiry', 'Импорт', pipeline_id=pipeline['id'], deal_id=deal['id'])
    create(db, user, 'inquiry', 'Сайт', customer={'temperature': 'warm'})
    assert service.list_entries(db, 'inquiry', temperature='warm')['total'] == 2
    assert service.list_entries(db, 'inquiry', temperature='cold')['total'] == 0
    summary = service.kanban_summary(db, pipeline_id=pipeline['id'], temperature='warm')
    assert sum(column['count'] for column in summary['columns']) == 2
    assert next(c for c in summary['columns'] if c['stage_id'] == stage['id'])['amounts'] == {'RUB': 150}


def test_91_agencies_and_90_links_are_preserved_and_not_promoted(db, make_user):
    user = make_user(admin=True)
    pipeline, new, connected = board(db, user, agency=True)
    for index in range(91):
        contact = create(db, user, 'contact', f'Контакт {index}') if index < 90 else None
        create(db, user, 'deal', f'АН {index}', pipeline_id=pipeline['id'], stage_id=new['id'],
            contact_ids=[contact['id']] if contact else [])
    before = [(row.id, row.version, deepcopy(row.payload)) for row in db.query(CRMEntity)]
    events = db.query(CRMEvent).count()
    users = db.query(User).count()
    assert agency_sources(db)['items'] == []
    assert sum(len(row.payload.get('contact_ids', [])) for row in db.query(CRMEntity).filter_by(kind='deal')) == 90
    assert [(row.id, row.version, row.payload) for row in db.query(CRMEntity)] == before
    assert db.query(CRMEvent).count() == events and db.query(User).count() == users


def test_agency_supports_ten_agents_and_roles_without_user_creation(db, make_user):
    user = make_user(admin=True)
    pipeline, new, _ = board(db, user, agency=True)
    contacts = [create(db, user, 'contact', f'Сотрудник {i}', phone=f'+7900000{i:04d}') for i in range(13)]
    roles = ['owner', 'decision_maker', 'marketer'] + ['agent'] * 10
    users = db.query(User).count()
    agency = create(db, user, 'deal', 'АН', pipeline_id=pipeline['id'], stage_id=new['id'],
        contact_ids=[c['id'] for c in contacts], agency_profile={'legal_status': 'ooo', 'members': [
            {'contact_id': c['id'], 'roles': [role]} for c, role in zip(contacts, roles)]})
    assert len(agency['data']['agency_profile']['members']) == 13
    assert db.query(User).count() == users
    outsider = create(db, user, 'contact', 'Чужой')
    with pytest.raises(HTTPException):
        change(db, user, agency, agency_profile={'members': [{'contact_id': outsider['id'], 'roles': ['agent']}]})


def test_referral_validates_membership_eligibility_and_preserves_historical_snapshot(db, make_user):
    user = make_user(admin=True)
    ap, new, connected = board(db, user, agency=True)
    sales, stage, _ = board(db, user)
    agent = create(db, user, 'contact', 'Агент до переименования', phone='+79000000000')
    outsider = create(db, user, 'contact', 'Другой агент')
    agency = create(db, user, 'deal', 'АН до переименования', pipeline_id=ap['id'], stage_id=new['id'], contact_ids=[agent['id']])
    source = {'agency_id': agency['id'], 'agent_contact_id': agent['id'], 'agency_name': 'Подмена'}
    with pytest.raises(HTTPException):
        create(db, user, 'deal', 'Дом', pipeline_id=sales['id'], stage_id=stage['id'], referral=source)
    agency = change(db, user, agency, stage_id=connected['id'])
    with pytest.raises(HTTPException):
        create(db, user, 'deal', 'Дом', pipeline_id=sales['id'], stage_id=stage['id'], referral={**source, 'agent_contact_id': outsider['id']})
    deal = create(db, user, 'deal', 'Дом', pipeline_id=sales['id'], stage_id=stage['id'], referral=source)
    assert deal['data']['referral']['agency_name'] == 'АН до переименования'
    service.patch(db, agency['id'], Patch(version=agency['version'], name='Новое имя АН', archived=True), user)
    service.patch(db, agent['id'], Patch(version=agent['version'], name='Новое имя агента'), user)
    updated = change(db, user, deal, amount=500, referral={**deal['data']['referral'], 'agency_name': 'Ещё подмена'})
    assert updated['data']['referral'] == deal['data']['referral']
    assert agency_sources(db)['total'] == 0


def test_agency_policy_is_admin_only_and_sources_use_clients_permission(db, make_user, api):
    admin = make_user(admin=True)
    pipeline, new, connected = board(db, admin, agency=True)
    manager = api(make_user(Module.CLIENTS, level=AccessLevel.FULL))
    assert manager.patch(f"/api/crm/entries/{new['id']}", json={'version': new['version'], 'data': {'agency_source_eligible': True}}).status_code == 403
    assert manager.get('/api/crm/agency-sources').status_code == 200
    outsider = api(make_user(Module.TASKS, level=AccessLevel.FULL))
    assert outsider.get('/api/crm/agency-sources').status_code == 403


def test_accept_preserves_existing_fields_and_customer_snapshot_without_overwriting_contact(db, make_user):
    user = make_user(admin=True)
    pipeline, stage, _ = board(db, user)
    contact = create(db, user, 'contact', 'Ранее известный', phone='+79000000000', customer={'city': 'Москва'})
    pending = create(db, user, 'deal', 'Скрытая', pipeline_id=pipeline['id'], stage_id=stage['id'], is_unsorted=True,
        contact_ids=[contact['id']], needs={'purchase_goal': 'renting'}, purchase_terms={'initial_payment': 123})
    inquiry = create(db, user, 'inquiry', 'Входящая', deal_id=pending['id'], phone='+79000000000',
        source_name='Исходный сайт', customer={'city': 'Тамбов', 'temperature': 'hot'})
    accepted = service.accept_inquiry(db, service.get(db, inquiry['id']), Entry(name='Принята', data={'pipeline_id': pipeline['id'], 'stage_id': stage['id']}), user)
    assert accepted['id'] == pending['id'] and accepted['data']['needs']['purchase_goal'] == 'renting'
    assert accepted['data']['purchase_terms']['initial_payment'] == 123
    assert accepted['data']['inquiry_customer_snapshot']['city'] == 'Тамбов'
    assert service.get(db, contact['id']).payload['customer']['city'] == 'Москва'
    assert service.get(db, inquiry['id']).payload['source_name'] == 'Исходный сайт'
