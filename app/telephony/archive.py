"""Replay the operator archive, including recordings delivered after call completion."""
from datetime import datetime, timedelta, timezone
import argparse
import re
import time
import httpx
from app.core.config import settings
from app.telephony import service


def history(client, start, end):
    domain = settings.megafon_domain
    if not re.fullmatch(r'vats[0-9]+\.megapbx\.ru', domain):
        raise ValueError('Укажите проверенный домен ВАТС')
    if not settings.megafon_api_token:
        raise ValueError('Ключ чтения истории не настроен')
    response = client.get(f'https://{domain}/crmapi/v1/history/json', params={
        'start': start.strftime('%Y%m%dT%H%M%SZ'), 'end': end.strftime('%Y%m%dT%H%M%SZ'),
        'type': 'all', 'limit': 5000,
    }, headers={'X-API-KEY': settings.megafon_api_token})
    response.raise_for_status()
    data = response.json()
    if isinstance(data, dict):
        data = data.get('items', data.get('data', data.get('history')))
    if not isinstance(data, list):
        raise ValueError('Неизвестный формат архива МегаФона')
    if len(data) >= 5000:
        if (end-start).total_seconds() <= 1:
            raise ValueError('Архив достиг лимита в одну секунду; нужна отдельная сверка')
        middle = start + timedelta(seconds=int((end-start).total_seconds() // 2))
        return history(client, start, middle) + history(client, middle, end)
    return data


def sync(db, start, end):
    count = 0
    with httpx.Client(timeout=90, trust_env=False, follow_redirects=False) as client:
        cursor = start
        while cursor < end:
            stop = min(cursor + timedelta(days=1), end)
            records = history(client, cursor, stop)
            for record in records:
                stamp = datetime.fromisoformat(record['start'].replace('Z','+00:00')).astimezone(timezone.utc)
                service.ingest(db, {
                    'cmd': 'history', 'callid': str(record['uid']), 'phone': record['client'],
                    'type': record['type'], 'user': record.get('user',''),
                    'start': stamp.strftime('%Y%m%dT%H%M%SZ'), 'duration': record.get('duration',0),
                    'status': record.get('status','unknown'), 'link': record.get('record',''),
                })
                count += 1
            cursor = stop
            time.sleep(1)
    return count


if __name__ == '__main__':
    from app.db import import_all_models  # noqa: F401
    from app.db.session import SessionLocal
    parser = argparse.ArgumentParser()
    parser.add_argument('--since', required=True, help='YYYY-MM-DD, UTC')
    parser.add_argument('--until', required=True, help='YYYY-MM-DD, UTC, exclusive')
    args = parser.parse_args()
    start = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(args.until).replace(tzinfo=timezone.utc)
    if end <= start:
        parser.error('Дата окончания должна быть позже начала')
    with SessionLocal() as db:
        count = sync(db,start,end)
    print(f'Archive records reconciled: {count}')
