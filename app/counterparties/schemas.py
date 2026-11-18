import re
from typing import Literal
from uuid import UUID, uuid4
from fastapi import HTTPException
from pydantic import Field, field_validator
from app.accounting.evidence_schemas import Strict


class Bank(Strict):
    id: UUID = Field(default_factory=uuid4)
    account_number: str = Field('', max_length=20)
    bik: str = Field('', max_length=9)
    bank_name: str = Field('', max_length=255)
    correspondent_account: str = Field('', max_length=20)
    bank_address: str = Field('', max_length=1000)
    primary: bool = False
    archived: bool = False


class Person(Strict):
    id: UUID = Field(default_factory=uuid4)
    name: str = Field('', max_length=255)
    phone: str = Field('', max_length=100)
    email: str = Field('', max_length=255)
    primary: bool = False
    archived: bool = False


class Card(Strict):
    name: str = Field('', max_length=255)
    inn: str = Field('', max_length=12)
    legal_type: Literal['', 'legal_entity', 'sole_proprietor', 'individual'] = ''
    kpp: str = Field('', max_length=9)
    ogrn: str = Field('', max_length=13)
    legal_address: str = Field('', max_length=2000)
    actual_address: str = Field('', max_length=2000)
    addresses_same: bool = False
    director: str = Field('', max_length=255)
    accounts: list[Bank] = Field(default_factory=list, max_length=50)
    contacts: list[Person] = Field(default_factory=list, max_length=50)

    @field_validator('name', 'inn', 'kpp', 'ogrn', 'legal_address', 'actual_address', 'director')
    @classmethod
    def trim(cls, value):
        return value.strip()


class Save(Strict):
    request_id: UUID
    etag: str = Field('', max_length=64)
    card: Card
    supplier: bool = True


class Apply(Strict):
    request_id: UUID
    preview_id: UUID
    fields: list[str] = Field(max_length=120)


def valid_inn(value):
    if not re.fullmatch(r'[0-9]{10}|[0-9]{12}', value):
        return False
    digits = list(map(int, value))
    check = lambda weights: sum(a*b for a, b in zip(weights, digits)) % 11 % 10
    if len(value) == 10:
        return check([2,4,10,3,5,9,4,6,8]) == digits[9]
    return check([7,2,4,10,3,5,9,4,6,8]) == digits[10] and check([3,7,2,4,10,3,5,9,4,6,8]) == digits[11]


def validate(card, previous=None):
    errors = {}
    if not card['name'].strip(): errors['name'] = 'Укажите наименование'
    # Old incomplete cards remain editable; a new/changed identity is validated fully.
    identity_changed = previous is None or any(card[k] != previous[k] for k in ('inn', 'legal_type'))
    if identity_changed:
        if not card['legal_type']: errors['legal_type'] = 'Выберите тип контрагента'
        if not valid_inn(card['inn']): errors['inn'] = 'Укажите ИНН с корректными контрольными цифрами'
        elif len(card['inn']) != (10 if card['legal_type'] == 'legal_entity' else 12): errors['inn'] = 'ИНН не соответствует типу контрагента'
    if card['legal_type'] != 'legal_entity' and (card['kpp'] or card['ogrn']):
        errors['legal_type'] = 'КПП и ОГРН доступны только юридическому лицу'
    if card['kpp'] and not re.fullmatch(r'[0-9]{4}[0-9A-Z]{2}[0-9]{3}', card['kpp']): errors['kpp'] = 'КПП должен содержать 9 знаков в формате ФНС'
    if card['ogrn'] and (not re.fullmatch(r'[0-9]{13}', card['ogrn']) or int(card['ogrn'][:-1]) % 11 % 10 != int(card['ogrn'][-1])): errors['ogrn'] = 'Проверьте ОГРН и контрольную цифру'
    if card['addresses_same'] and card['legal_address'] != card['actual_address']: errors['actual_address'] = 'Адреса различаются: снимите отметку совпадения'
    for kind in ('accounts', 'contacts'):
        ids = [r['id'] for r in card[kind]]
        if len(ids) != len(set(ids)): errors[kind] = 'Повтор идентификатора строки'
        if sum(bool(r['primary'] and not r['archived']) for r in card[kind]) > 1: errors[kind] = 'Основной может быть только одна строка'
    seen = set()
    for i, row in enumerate(card['accounts']):
        if row['archived']: continue
        prefix = f'accounts.{i}.'
        account, bik = row['account_number'], row['bik']
        if not re.fullmatch(r'[0-9]{20}', account): errors[prefix+'account_number'] = 'Расчётный счёт: 20 цифр'
        if not re.fullmatch(r'[0-9]{9}', bik): errors[prefix+'bik'] = 'БИК: 9 цифр'
        if not row['bank_name'].strip(): errors[prefix+'bank_name'] = 'Укажите банк'
        for field, lead in [('account_number', bik[-3:]), ('correspondent_account', '0'+bik[4:6])]:
            value = row[field]
            if value and (not re.fullmatch(r'[0-9]{20}', value) or (len(bik)==9 and bik.isascii() and bik.isdigit() and sum(int(n)*[7,1,3][j%3] for j,n in enumerate(lead+value)) % 10)):
                errors[prefix+field] = 'Счёт не проходит проверку с указанным БИК'
        key = (bik, account)
        if key in seen: errors[prefix+'account_number'] = 'Этот счёт уже добавлен'
        seen.add(key)
    from email_validator import validate_email, EmailNotValidError
    for i, row in enumerate(card['contacts']):
        if row['archived']: continue
        if previous and row in previous['contacts']: continue
        if row['email']:
            try: validate_email(row['email'], check_deliverability=False)
            except EmailNotValidError: errors[f'contacts.{i}.email'] = 'Проверьте email'
        if row['phone'] and not re.fullmatch(r'[+0-9()\s.\-]{5,40}', row['phone']): errors[f'contacts.{i}.phone'] = 'Проверьте телефон'
    if errors: raise HTTPException(422, {'detail':'Проверьте выделенные поля', 'fields':errors})
