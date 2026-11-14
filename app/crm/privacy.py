"""Default-closed family/passport boundary. No new roles or grants are created."""
from copy import deepcopy
import re
from fastapi import HTTPException
from app.users.models import UserRole

PRIVATE_CUSTOMER_FIELDS = frozenset({'birth_date', 'marital_status', 'marriage_date', 'children_birth_dates'})
PRIVATE_KEYS = PRIVATE_CUSTOMER_FIELDS | frozenset({'passport', 'passport_file_id', 'passport_url', 'passport_number',
    'passport_scan', 'passport_documents', 'passport_series', 'passport_issued_by', 'passport_issue_date'})
PUBLIC_CUSTOM_CODES = frozenset({'PHONE', 'EMAIL'})
PUBLIC_CUSTOM_LABELS = frozenset({'телефон', 'email', 'e-mail', 'почта', 'город', 'площадь дома', 'тип объекта',
    'количество комнат', 'отделка', 'бюджет', 'источник', 'форма оплаты', 'цель покупки', 'срок выхода на сделку'})
PRIVATE_LABEL = re.compile(r'паспорт|passport|семейн|marital|супруг|spouse|брак|marriage|реб[её]н|дет[еиья]|children|child.?birth|дата.?рожд|birth.?date|birthday', re.I)


def can_read_private(user):
    return bool(user and user.role == UserRole.ADMIN)


def public_custom_field(field):
    if not isinstance(field, dict):
        return False
    label = str(field.get('field_name') or field.get('name') or '').strip().casefold()
    code = str(field.get('field_code') or '').upper()
    if PRIVATE_LABEL.search(label) or PRIVATE_LABEL.search(code):
        return False
    return code in PUBLIC_CUSTOM_CODES or label in PUBLIC_CUSTOM_LABELS


def redact(value, user):
    if can_read_private(user):
        return deepcopy(value)
    if isinstance(value, list):
        return [redact(item, user) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key in PRIVATE_KEYS or PRIVATE_LABEL.search(key) or key == 'inquiry_customer_snapshot':
            continue
        if key in ('custom_fields', 'custom_fields_values'):
            result[key] = [redact(field, user) for field in item if public_custom_field(field)] if isinstance(item, list) else []
        else:
            result[key] = redact(item, user)
    return result


def prepare_write(data, user, previous=None):
    """Check supplied values before schema validation; preserve hidden old fields."""
    if data is None or can_read_private(user):
        return data
    previous = previous or {}
    def guard(value):
        if isinstance(value, list):
            for item in value:
                guard(item)
        elif isinstance(value, dict):
            for key, item in value.items():
                if key in PRIVATE_KEYS or PRIVATE_LABEL.search(key) or key == 'inquiry_customer_snapshot':
                    raise HTTPException(403, 'Закрытые персональные поля доступны только администратору')
                if key in ('custom_fields', 'custom_fields_values'):
                    if not isinstance(item, list) or any(not public_custom_field(field) for field in item):
                        raise HTTPException(403, 'Непроверенные дополнительные поля доступны только администратору')
                guard(item)
    guard(data)
    old_customer = previous.get('customer') or {}
    if 'customer' in data and data['customer'] is None and any(old_customer.get(key) not in (None, '', []) for key in PRIVATE_CUSTOMER_FIELDS):
        raise HTTPException(403, 'Нельзя стереть закрытые персональные поля')
    result = deepcopy(data)
    for key in ('custom_fields', 'custom_fields_values'):
        if key not in data:
            continue
        hidden = [deepcopy(field) for field in (previous.get(key) or []) if not public_custom_field(field)]
        private_ids = {str(field['field_id']) for field in hidden if isinstance(field, dict) and field.get('field_id') is not None}
        if any(str(field.get('field_id')) in private_ids for field in data[key]):
            raise HTTPException(403, 'Нельзя изменять закрытые дополнительные поля')
        result[key] = hidden + result[key]
    return result


def private_tokens(value):
    tokens = set()
    def collect(item):
        if isinstance(item, str) and len(item.strip()) >= 3:
            tokens.add(item.casefold())
        elif isinstance(item, (int, float)) and not isinstance(item, bool) and len(str(item)) >= 3:
            tokens.add(str(item))
        elif isinstance(item, dict):
            for child in item.values():
                collect(child)
        elif isinstance(item, list):
            for child in item:
                collect(child)
    if isinstance(value, dict):
        for key, item in value.items():
            if key in PRIVATE_KEYS or PRIVATE_LABEL.search(key) or key == 'inquiry_customer_snapshot':
                collect(item)
            elif key in ('custom_fields', 'custom_fields_values') and isinstance(item, list):
                for field in item:
                    if not public_custom_field(field):
                        collect(field)
            else:
                tokens.update(private_tokens(item))
    elif isinstance(value, list):
        for child in value:
            tokens.update(private_tokens(child))
    return tokens


def visible_history(entries, row, user):
    if can_read_private(user):
        return entries
    secrets = private_tokens(row.payload)
    for entry in entries:
        secrets.update(private_tokens(entry.get('details', {})))
    result = []
    for entry in entries:
        # Imported entries are already reduced by public_history_entry; raw
        # imported details are never passed here for a non-administrator.
        cleaned = redact(entry, user)
        text = str(cleaned.get('text') or '')
        if PRIVATE_LABEL.search(text) or any(token in text.casefold() for token in secrets):
            continue
        result.append(cleaned)
    return result


