from app.common.module_access import Module


def test_calculate_exact_35_percent_margin_and_minimum_price(api, make_user):
    client = api(make_user(Module.CLIENTS))
    payload = {'sale_price': '100.00', 'materials': '30', 'labor': '15', 'purchase': '0',
               'other_variable': '10', 'fixed_per_month': '100', 'planned_items': 10}
    result = client.post('/api/clients/margin-preview', json=payload)
    assert result.status_code == 200
    assert result.json() == {'status': 'meets_target', 'missing': [], 'target_margin_percent': '35',
        'cost': '65.00', 'fixed_share': '10.00', 'profit': '35.00', 'margin_percent': '35.00',
        'markup_percent': '53.85', 'minimum_price': '100.00'}
    payload['sale_price'] = '99.99'
    assert client.post('/api/clients/margin-preview', json=payload).json()['status'] == 'below_target'
    payload['materials'] = None
    assert client.post('/api/clients/margin-preview', json=payload).json()['status'] == 'incomplete'
    payload['planned_items'] = 0
    assert client.post('/api/clients/margin-preview', json=payload).status_code == 422


def test_minimum_price_rounds_up_and_access_is_required(api, make_user):
    payload = {'sale_price': '0.03', 'materials': '0.02', 'labor': '0', 'purchase': '0',
               'other_variable': '0', 'fixed_per_month': '0', 'planned_items': 1}
    result = api(make_user(Module.CLIENTS)).post('/api/clients/margin-preview', json=payload).json()
    assert result['minimum_price'] == '0.04'
    assert result['status'] == 'below_target'
    assert api(make_user()).post('/api/clients/margin-preview', json=payload).status_code == 403
