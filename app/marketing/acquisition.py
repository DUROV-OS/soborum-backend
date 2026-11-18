"""Safe display labels from saved receipt markers, independent of goal matching."""
import re

DIRECTIONS = {'houses': 'Модульные дома', 'flat': 'Флэт', 'barn': 'Барнхаусы',
              'bathhouse': 'Бани', 'glamping': 'Дома для баз отдыха',
              'brand': 'Наш бренд', 'competitor': 'Сравнение производителей',
              'designing': 'Проектирование', 'retargeting': 'Возврат посетителей'}


def direction_label(utm):
    parts = str((utm or {}).get('utm_content') or '').split('.')
    if (len(parts) == 4 and all(re.fullmatch(r'[0-9]{1,25}', p) for p in parts[:2])
            and parts[2] in ('search', 'context')):
        return DIRECTIONS.get(parts[3], 'Не определено')
    return 'Не определено'


def acquisition(utm=None, tracking=None, source='Сайт'):
    """Never infer Direct from the counter, page, customer, or sales stage."""
    utm, tracking = utm or {}, tracking or {}
    marker_source = str(utm.get('utm_source') or '').strip().lower()
    medium = str(utm.get('utm_medium') or '').strip().lower()
    campaign = str(utm.get('utm_campaign') or '').strip()
    campaign = campaign if re.fullmatch(r'[0-9]{1,20}', campaign) else None
    yclid = bool(re.fullmatch(r'[0-9]{1,64}', str(tracking.get('yclid') or '')))
    yandex = marker_source in ('yandex', 'ya', 'direct', 'yandex_direct')
    utm_direct = yandex and medium == 'cpc' and bool(campaign)
    # Contradicting stored sources need a person to inspect the receipt.
    direct = (utm_direct or yclid) and (not marker_source or yandex)
    basis = ('UTM и yclid' if utm_direct and yclid else 'UTM' if utm_direct else 'yclid') if direct else None
    return {'is_direct': bool(direct), 'source_label': 'Яндекс Директ' if direct else source,
            'campaign_id': campaign if direct else None,
            'direction': direction_label(utm) if direct else 'Не определено',
            'basis': basis,
            'has_visit_id': bool(yclid or re.fullmatch(r'[0-9]{1,64}', str(tracking.get('client_id') or '')))}
