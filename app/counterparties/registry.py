from typing import Literal
from uuid import UUID

from fastapi import HTTPException
from pydantic import Field, model_validator
from sqlalchemy import select

from app.accounting.evidence_schemas import Citation, Strict
from app.accounting.models import Counterparty, CounterpartyKind, SupplierOrder, SupplierOrderStatus
from app.clients.models import Client, ContractSource
from app.common.files import FileAsset, FilePurpose, file_integrity
from app.common.module_access import AccessLevel, Module
from app.purchasing import controls
from app.warehouse.models import Supplier, SupplierStatus
from . import service
from .models import Change, Contact, Details

Role = Literal['supplier', 'client']
RegistryStatus = Literal['supplier', 'client', 'deal_closed']
Sort = Literal['name', 'inn', 'phone', 'status', 'comment']
STATUS_ORDER = {'supplier': 1, 'client': 2, 'deal_closed': 3}
RUSSIAN_ORDER = {ord(letter): 0x0410 + index for index, letter in enumerate('абвгдеёжзийклмнопрстуфхцчшщъыьэюя')}
COMMENT_LIMIT = 4000


class RegistryEdit(Strict):
    request_id: UUID
    etag: str = Field(min_length=64, max_length=64)
    status: RegistryStatus | None = None
    comment: str | None = Field(None, max_length=COMMENT_LIMIT)
    profile_id: int | None = Field(None, gt=0)
    evidence: Citation | None = None
    signed: bool = False

    @model_validator(mode='after')
    def changes(self):
        if self.status is None and self.comment is None:
            raise ValueError('Укажите статус или комментарий')
        if self.profile_id is not None and self.status not in ('supplier', 'client'):
            raise ValueError('Выберите роль для привязки профиля')
        if (self.evidence or self.signed) and self.status != 'deal_closed':
            raise ValueError('Основание указывается при заключении сделки')
        return self


def visible_rows(db, user):
    if not service.allowed(user):
        raise HTTPException(403, 'Нет доступа к справочнику контрагентов')
    query = db.query(Counterparty)
    if user.access_level(Module.ACCOUNTING) < AccessLevel.VIEW:
        query = query.filter(Counterparty.supplier_id.is_not(None))
    return query


def text_key(value):
    return value.strip().casefold()


def russian_key(value):
    return text_key(value).translate(RUSSIAN_ORDER)


def digits(value):
    return ''.join(char for char in value if char in '0123456789')


def list_rows(db, user, *, q, name, inn, kpp, phone, statuses, comment, sort, direction, archive, offset, limit):
    visible = visible_rows(db, user)
    if archive != 'all':
        visible = visible.filter(Counterparty.is_active.is_(archive == 'active'))
    rows = visible.add_entity(Details).add_columns(Supplier.contacts).outerjoin(
        Details, Details.counterparty_id == Counterparty.id
    ).outerjoin(Supplier, Supplier.id == Counterparty.supplier_id).all()
    client_phones = {}
    if user.access_level(Module.CLIENTS) >= AccessLevel.VIEW:
        client_phones = dict(db.query(Client.id, Client.phone).filter(
            Client.id.in_(visible.with_entities(Counterparty.client_id).statement)
        ).all())
    contacts = {}
    for row in db.query(Contact).filter(Contact.counterparty_id.in_(
        visible.with_entities(Counterparty.id).statement
    )).order_by(Contact.id):
        contacts.setdefault(row.counterparty_id, []).append(row.data)

    term, name, inn, kpp, comment = map(text_key, (q, name, inn, kpp, comment))
    if '/' in inn and not kpp:
        inn, kpp = (value.strip() for value in inn.split('/', 1))
    phone_filter = digits(phone)
    matched, empty = [], []
    for cp, details, supplier_contacts in rows:
        status = service.registry_status(cp, details)
        row_name, row_inn = text_key(cp.name), cp.inn or ''
        row_kpp = text_key((details.data.get('kpp') or '') if details else '')
        row_comment = text_key(cp.comment or '')
        if (term and term not in row_name and term not in row_inn
                or name not in row_name or inn not in row_inn or kpp not in row_kpp
                or comment not in row_comment or statuses and status not in statuses):
            continue
        phones = contacts.get(cp.id, [])
        if not phones and service.uses_legacy_contacts(details):
            phones = service.legacy_contacts(cp, supplier_contacts)
        number = digits(service.primary_phone(phones) or client_phones.get(cp.client_id, ''))
        if phone.strip() and (not phone_filter or phone_filter not in number):
            continue
        keys = {
            'name': russian_key(cp.name),
            'inn': (row_inn, row_kpp) if row_inn or row_kpp else None,
            'phone': number,
            'status': STATUS_ORDER.get(status),
            'comment': russian_key(row_comment),
        }
        key = keys[sort]
        (matched if key else empty).append((cp, key))

    matched.sort(key=lambda pair: pair[0].id)
    matched.sort(key=lambda pair: pair[1], reverse=direction == 'desc')
    empty.sort(key=lambda pair: pair[0].id)
    ordered = matched + empty
    return {
        'items': [service.out(db, cp, user) for cp, _ in ordered[offset:offset + limit]],
        'total': len(ordered),
        'can_create': service.allowed(user, edit=True),
    }


def require_profile_access(user, role):
    module = Module.WAREHOUSE if role == 'supplier' else Module.CLIENTS
    if user.access_level(Module.ACCOUNTING) < AccessLevel.EDIT or user.access_level(module) < AccessLevel.VIEW:
        raise HTTPException(403, 'Для привязки нужны права бухгалтерии и доступ к профилю')


def available_profiles(db, user, role, q):
    require_profile_access(user, role)
    model, title, link = (Supplier, Supplier.name, Counterparty.supplier_id) if role == 'supplier' else (
        Client, Client.full_name, Counterparty.client_id
    )
    query = db.query(model.id, title).filter(~model.id.in_(select(link).where(link.is_not(None))))
    if q.strip():
        query = query.filter(title.icontains(q.strip(), autoescape=True))
    return [{'id': id, 'name': label} for id, label in query.order_by(title, model.id).limit(30)]


def bind_profile(db, cp, user, role, id):
    require_profile_access(user, role)
    if service.base_role(cp):
        raise HTTPException(409, 'У контрагента уже есть связанный профиль')
    model, field = (Supplier, Counterparty.supplier_id) if role == 'supplier' else (Client, Counterparty.client_id)
    if db.query(model).filter(model.id == id).with_for_update().first() is None:
        raise HTTPException(404, 'Профиль не найден')
    if db.query(Counterparty.id).filter(field == id).first():
        raise HTTPException(409, 'Профиль уже связан с другим контрагентом')
    if role == 'supplier':
        cp.supplier_id = id
    else:
        cp.client_id = id
    cp.kind = CounterpartyKind(role)


def confirmation(db, cp, user, body):
    role = service.base_role(cp)
    if role == 'supplier':
        if user.access_level(Module.ACCOUNTING) < AccessLevel.EDIT:
            raise HTTPException(403, 'Договор поставщика подтверждает сотрудник с правом редактирования бухгалтерии')
        if not body.evidence or not body.signed:
            raise HTTPException(422, 'Выберите подписанный договор и подтвердите проверку подписей')
        asset = controls.documents(db, cp.supplier_id).get(body.evidence.file_id)
        locator = body.evidence.locator
    elif role == 'client':
        if user.access_level(Module.CLIENTS) < AccessLevel.VIEW:
            raise HTTPException(403, 'Для подтверждения сделки нужен доступ к карточке клиента')
        client = db.get(Client, cp.client_id)
        if not client or client.contract_source != ContractSource.UPLOADED or not client.contract_verified_at or not client.contract_verified_by_id:
            raise HTTPException(422, 'Проверьте подписанный договор в карточке клиента')
        asset = db.get(FileAsset, client.contract_file_id) if client.contract_file_id else None
        if asset and asset.purpose != FilePurpose.CONTRACT:
            asset = None
        locator = 'Проверенный договор клиента'
    else:
        raise HTTPException(422, 'Сначала привяжите профиль поставщика или клиента')
    if not asset or file_integrity(asset) != 'matches':
        raise HTTPException(422, 'Загрузите подтверждающий документ этого контрагента с проверяемой контрольной суммой')
    return {
        'role': role, 'profile_id': cp.supplier_id or cp.client_id,
        'file_id': asset.id, 'filename': asset.filename, 'locator': locator, 'sha256': asset.sha256,
    }


def edit(db, user, id, body):
    service.lock(db)
    request = {'id': id, 'registry': body.model_dump(mode='json')}
    replay = service.replay(db, user, body.request_id, request)
    if replay:
        return replay
    cp = service.get(db, id, user, edit=True)
    before = service.out(db, cp, user)
    if body.etag != before['etag']:
        raise HTTPException(409, {'detail': 'Карточку изменил другой сотрудник', 'current': before})
    profile = db.get(Details, id)
    proof = None
    if body.status == 'deal_closed':
        proof = confirmation(db, cp, user, body)
    elif body.status:
        if body.profile_id is not None:
            bind_profile(db, cp, user, body.status, body.profile_id)
        if body.status != service.base_role(cp):
            raise HTTPException(422, 'Сначала привяжите соответствующий профиль в карточке')
    if body.status:
        if profile is None:
            profile = Details(counterparty_id=id, version=0, data={})
            db.add(profile)
        profile.data = {**profile.data, 'deal_confirmation': proof}
        profile.version += 1
    if body.comment is not None:
        cp.comment = body.comment or None
    db.flush()
    after = service.out(db, cp, user)
    db.add(Change(
        counterparty_id=id, actor_id=user.id, request_id=str(body.request_id),
        request_hash=service.digest(request),
        data={'kind': 'registry_edit', 'before': before, 'after': after,
              'source': {'kind': 'document', 'proof': proof} if proof else {'kind': 'manual'}},
    ))
    db.commit()
    return after


class ArchiveCounterparty(Strict):
    request_id: UUID
    etag: str = Field(min_length=64, max_length=64)
    archived: bool


def archive_counterparty(db, user, id, body):
    service.lock(db)
    request = {'id': id, 'archive': body.model_dump(mode='json')}
    repeated = service.replay(db, user, body.request_id, request)
    if repeated:
        return repeated
    cp = service.get(db, id, user, edit=True)
    before = service.out(db, cp, user)
    if body.etag != before['etag']:
        raise HTTPException(409, 'Карточку изменил другой сотрудник. Обновите список.')
    if cp.is_active == (not body.archived):
        return before

    if cp.supplier_id:
        supplier = db.query(Supplier).filter_by(id=cp.supplier_id).populate_existing().with_for_update().first()
        if supplier is None or db.query(Counterparty.id).filter(
            Counterparty.supplier_id == cp.supplier_id, Counterparty.id != cp.id,
        ).first():
            raise HTTPException(409, 'Сначала проверьте связь карточки с поставщиком.')
        if body.archived and db.query(SupplierOrder.id).filter(
            SupplierOrder.supplier_id == supplier.id,
            SupplierOrder.archived.is_(False),
            SupplierOrder.status != SupplierOrderStatus.RECEIVED,
        ).first():
            raise HTTPException(409, 'У поставщика есть незавершённые заказы. Завершите их перед удалением.')
        supplier.status = SupplierStatus.ARCHIVED if body.archived else SupplierStatus.ACTIVE

    cp.is_active = not body.archived
    profile = db.get(Details, id)
    if profile is None:
        profile = Details(counterparty_id=id, version=0, data={})
        db.add(profile)
    profile.version += 1
    db.flush()
    after = service.out(db, cp, user)
    db.add(Change(
        counterparty_id=id, actor_id=user.id, request_id=str(body.request_id),
        request_hash=service.digest(request),
        data={'kind': 'archive' if body.archived else 'restore', 'before': before,
              'after': after, 'source': {'kind': 'manual'}},
    ))
    db.commit()
    return after


def can_read_proof(user, proof):
    module = Module.ACCOUNTING if proof['role'] == 'supplier' else Module.CLIENTS
    return user.access_level(module) >= AccessLevel.VIEW


def visible_change(user, data):
    source = data.get('source', {})
    proof = source.get('proof')
    if proof and not can_read_proof(user, proof):
        return {**data, 'source': {'kind': source['kind']}}
    return data


def history_asset(db, user, cp, change_id):
    change = db.get(Change, change_id)
    if not change or change.counterparty_id != cp.id:
        raise HTTPException(404, 'Основание не найдено')
    proof = change.data.get('source', {}).get('proof')
    if not proof:
        raise HTTPException(404, 'Основание не найдено')
    if not can_read_proof(user, proof):
        raise HTTPException(403, 'Нет доступа к подтверждающему документу')
    if proof['role'] == 'supplier':
        asset = controls.documents(db, proof['profile_id']).get(proof['file_id'])
    else:
        asset = db.get(FileAsset, proof['file_id'])
    if not asset or asset.sha256 != proof['sha256']:
        raise HTTPException(409, 'Версия документа изменилась или недоступна')
    return asset
