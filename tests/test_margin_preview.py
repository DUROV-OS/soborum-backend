from app.common.module_access import Module


def test_calculate_exact_35_percent_margin_and_minimum_price(api, make_user):
    client = api(make_user(Module.CLIENTS))
    payload = {'sale_price': '100.00', 'materials': '30', 'labor': '15', 'purchase': '0',
               'other_variable': '10', 'advertising': '0', 'realtor_percent': '0', 'salesperson_percent': '0', 'fixed_per_month': '100', 'planned_items': 10}
    result = client.post('/api/clients/margin-preview', json=payload)
    assert result.status_code == 200
    assert result.json() == {'status': 'meets_target', 'missing': [], 'target_margin_percent': '35',
        'cost': '65.00', 'fixed_share': '10.00', 'realtor_fee': '0.00', 'salesperson_fee': '0.00', 'profit': '35.00', 'margin_percent': '35.00',
        'markup_percent': '53.85', 'minimum_price': '100.00'}
    payload['sale_price'] = '99.99'
    assert client.post('/api/clients/margin-preview', json=payload).json()['status'] == 'below_target'
    payload['materials'] = None
    assert client.post('/api/clients/margin-preview', json=payload).json()['status'] == 'incomplete'
    payload['planned_items'] = 0
    assert client.post('/api/clients/margin-preview', json=payload).status_code == 422


def test_minimum_price_rounds_up_and_access_is_required(api, make_user):
    payload = {'sale_price': '0.03', 'materials': '0.02', 'labor': '0', 'purchase': '0',
               'other_variable': '0', 'advertising': '0', 'realtor_percent': '0', 'salesperson_percent': '0', 'fixed_per_month': '0', 'planned_items': 1}
    result = api(make_user(Module.CLIENTS)).post('/api/clients/margin-preview', json=payload).json()
    assert result['minimum_price'] == '0.04'
    assert result['status'] == 'below_target'
    assert api(make_user()).post('/api/clients/margin-preview', json=payload).status_code == 403


def test_commissions_and_attributed_advertising_raise_minimum_price(api, make_user):
    client = api(make_user(Module.CLIENTS))
    payload = {'sale_price': '2363636.37', 'materials': '500000', 'labor': '300000', 'purchase': '0',
               'other_variable': '150000', 'advertising': '50000', 'realtor_percent': '5',
               'salesperson_percent': '3', 'fixed_per_month': '3000000', 'planned_items': 10}
    result = client.post('/api/clients/margin-preview', json=payload).json()
    assert result['status'] == 'meets_target'
    # Fixed cost is 1.3m; 8% commissions change the denominator to 0.57.
    assert result['minimum_price'] == '2280701.76'
    assert result['realtor_fee'] == '118181.82'
    assert result['salesperson_fee'] == '70909.09'
    payload['sale_price'] = '2280701.75'
    assert client.post('/api/clients/margin-preview', json=payload).json()['status'] == 'below_target'
    payload['realtor_percent'] = None
    assert client.post('/api/clients/margin-preview', json=payload).json()['status'] == 'incomplete'
    payload['realtor_percent'] = '62'
    assert client.post('/api/clients/margin-preview', json=payload).json()['status'] == 'infeasible'
    payload['realtor_percent'] = '100.01'
    assert client.post('/api/clients/margin-preview', json=payload).status_code == 422


def test_equal_to_65_percent_commission_and_zero_cost_has_a_positive_minimum(api, make_user):
    payload = {'sale_price': '0.01', 'materials': '0', 'labor': '0', 'purchase': '0',
               'other_variable': '0', 'advertising': '0', 'realtor_percent': '65', 'salesperson_percent': '0',
               'fixed_per_month': '0', 'planned_items': 1}
    result = api(make_user(Module.CLIENTS)).post('/api/clients/margin-preview', json=payload).json()
    assert result['minimum_price'] == '0.01'
    assert result['status'] == 'meets_target'


def test_rounded_margin_cannot_hide_a_below_threshold_sale(api, make_user):
    payload = {'sale_price': '1000000', 'materials': '650000.01', 'labor': '0', 'purchase': '0',
               'other_variable': '0', 'advertising': '0', 'realtor_percent': '0', 'salesperson_percent': '0',
               'fixed_per_month': '0', 'planned_items': 1}
    result = api(make_user(Module.CLIENTS)).post('/api/clients/margin-preview', json=payload).json()
    assert result['margin_percent'] == '35.00'  # display rounded
    assert result['status'] == 'below_target'   # exact arithmetic decides
    assert result['minimum_price'] == '1000000.02'


def test_minimum_price_is_positive_when_all_recorded_costs_are_zero(api, make_user):
    payload = {'sale_price': '0', 'materials': '0', 'labor': '0', 'purchase': '0',
               'other_variable': '0', 'advertising': '0', 'realtor_percent': '0', 'salesperson_percent': '0',
               'fixed_per_month': '0', 'planned_items': 1}
    result = api(make_user(Module.CLIENTS)).post('/api/clients/margin-preview', json=payload).json()
    assert result['minimum_price'] == '0.01'
    assert result['status'] == 'below_target'
