"""One persistent assignment per caller. PBX handles ringing and overflow."""
import re
from datetime import datetime, timezone
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from app.common.module_access import AccessLevel, Module
from app.clients.models import Client
from app.crm.models import CRMEntity
from app.users.models import User
from app.telephony.models import PhoneRoutingConfig, PhoneRoutingMember, PhoneRoutingMemory, PhoneRoutingAudit


def normalize(value):
    raw = str(value or '').strip()
    if not re.fullmatch(r'\+?[0-9 ().-]+', raw):
        return ''
    digits = re.sub(r'\D', '', raw)
    if len(digits) == 10:
        digits = '7' + digits
    elif len(digits) == 11 and digits.startswith('8'):
        digits = '7' + digits[1:]
    return digits if re.fullmatch(r'[1-9][0-9]{9,14}', digits) else ''


def numbers(entry):
    customer = entry.payload.get('customer') or {}
    if not isinstance(customer, dict):
        customer = {}
    return {value for value in (
        normalize(entry.payload.get('phone')),
        normalize(customer.get('phone_2')), normalize(customer.get('phone_3')),
    ) if value}


class Member(BaseModel):
    model_config = ConfigDict(extra='forbid')
    user_id: int = Field(ge=1)
    pbx_user: str = Field(pattern=r'^[a-zA-Z0-9_.-]{1,100}$')
    enabled: bool = True


class Configuration(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: int = Field(ge=1)
    enabled: bool = False
    main_phone: str = '79066773555'
    wait_seconds: int = Field(20, ge=10, le=120)
    pbx_setup_confirmed: bool = False
    auto_create_leads: bool = False
    max_incoming_enabled: bool = False
    lead_stage_id: int | None = Field(None, ge=1)
    members: list[Member] = Field(default_factory=list, max_length=100)

    @field_validator('main_phone')
    @classmethod
    def valid_phone(cls, value):
        value = normalize(value)
        if not value:
            raise ValueError('Укажите основной номер с кодом страны')
        return value

    @model_validator(mode='after')
    def unique_members(self):
        if len({m.user_id for m in self.members}) != len(self.members) or len({m.pbx_user for m in self.members}) != len(self.members):
            raise ValueError('Сотрудник и логин ВАТС должны быть уникальными')
        if self.enabled and (not self.pbx_setup_confirmed or not any(m.enabled for m in self.members)):
            raise ValueError('Сопоставьте менеджеров и настройте основной номер и переадресацию в ВАТС')
        if (self.auto_create_leads or self.max_incoming_enabled) and self.lead_stage_id is None:
            raise ValueError('Выберите этап для новых заявок по звонку')
        return self


def locked_config(db):
    # UPDATE obtains the same transactional lock on PostgreSQL and SQLite.
    # A migration seeds row 1; lazy creation also supports fresh test databases.
    if db.get(PhoneRoutingConfig, 1) is None:
        try:
            with db.begin_nested():
                db.add(PhoneRoutingConfig(id=1))
                db.flush()
        except IntegrityError:
            pass
    db.execute(update(PhoneRoutingConfig).where(PhoneRoutingConfig.id == 1)
               .values(cursor=PhoneRoutingConfig.cursor))
    return db.query(PhoneRoutingConfig).filter_by(id=1).populate_existing().one()


def read_config(db):
    config = db.get(PhoneRoutingConfig, 1)
    return {
        'version': config.version if config else 1,
        'enabled': config.enabled if config else False,
        'main_phone': config.main_phone if config else '79066773555',
        'wait_seconds': config.wait_seconds if config else 20,
        'pbx_setup_confirmed': config.pbx_setup_confirmed if config else False,
        'auto_create_leads': config.auto_create_leads if config else False,
        'max_incoming_enabled': config.max_incoming_enabled if config else False,
        'lead_stage_id': config.lead_stage_id if config else None,
        'fallback': 'other_manager_keep_owner',
        'members': [dict(user_id=m.user_id, pbx_user=m.pbx_user, enabled=m.enabled)
                    for m in db.query(PhoneRoutingMember).order_by(PhoneRoutingMember.user_id)],
    }


def eligible(user):
    return user and user.is_active and user.access_level(Module.CLIENTS) >= AccessLevel.EDIT


def member_identifiers(member, user):
    # The provider accepts the PBX login for routing/dialling but can report
    # only the extension in live events. Extensions are admin-managed user data.
    return {value for value in (member.pbx_user, user.telephony_extension) if value}


def save_config(db, body, actor):
    config = locked_config(db)
    if body.version != config.version:
        raise HTTPException(409, 'Настройки уже изменены. Обновите страницу.')
    identifiers = set()
    for member in body.members:
        member_user = db.get(User, member.user_id)
        if member_user is None:
            raise HTTPException(422, 'Учётная запись участника не найдена')
        if member.enabled and not eligible(member_user):
            raise HTTPException(422, 'Участнику нужны активная учётная запись и право редактирования клиентов')
        aliases = member_identifiers(member, member_user)
        if identifiers.intersection(aliases):
            raise HTTPException(422, 'Логин и внутренний номер ВАТС не должны указывать на разных сотрудников')
        identifiers.update(aliases)
    before = read_config(db)
    if body.auto_create_leads or body.max_incoming_enabled:
        from app.telephony.leads import destination
        if destination(db, body.lead_stage_id) is None:
            raise HTTPException(422, 'Выберите открытый этап действующей воронки продаж')
    # Remove first so changing/swapping logins does not collide with old rows.
    db.query(PhoneRoutingMember).delete(synchronize_session=False)
    db.flush()
    db.add_all([PhoneRoutingMember(**m.model_dump()) for m in body.members])
    config.enabled = body.enabled
    config.main_phone = body.main_phone
    config.wait_seconds = body.wait_seconds
    config.pbx_setup_confirmed = body.pbx_setup_confirmed
    config.auto_create_leads = body.auto_create_leads
    if body.max_incoming_enabled and not config.max_incoming_enabled:
        config.max_leads_started_at = datetime.now(timezone.utc)
    config.max_incoming_enabled = body.max_incoming_enabled
    config.lead_stage_id = body.lead_stage_id
    if body.auto_create_leads or body.max_incoming_enabled:
        config.lead_actor_id = actor.id
    config.version += 1
    # Memory and the allocation cursor intentionally survive edits/restarts.
    db.add(PhoneRoutingAudit(actor_id=actor.id, details={'before': before, 'after': body.model_dump()}))
    db.commit()
    return read_config(db)


def crm_owner(db, number):
    """All matching records must agree. Shared numbers never merge clients."""
    parties = [e for e in db.query(CRMEntity).filter(
        CRMEntity.kind.in_(['contact', 'company', 'inquiry']), CRMEntity.archived.is_(False)
    ) if number in numbers(e)]
    party_ids = {e.id for e in parties}
    legacy = [c for c in db.query(Client) if normalize(c.phone) == number]
    legacy_ids = {c.id for c in legacy}
    owners = {c.manager_id for c in legacy if c.manager_id}
    owners.update(e.payload.get('responsible_user_id') for e in parties if e.payload.get('responsible_user_id'))
    for deal in db.query(CRMEntity).filter_by(kind='deal', archived=False):
        data = deal.payload
        if (party_ids.intersection(data.get('contact_ids') or []) or data.get('company_id') in party_ids
                or data.get('client_id') in legacy_ids or number in numbers(deal)):
            if data.get('responsible_user_id'):
                owners.add(data['responsible_user_id'])
    owners = {value for value in owners if isinstance(value, int) and not isinstance(value, bool)}
    ambiguous = len(owners) > 1 or len(legacy) > 1
    # Multiple unassigned cards cannot identify one client either.
    ambiguous = ambiguous or (len(parties) > 1 and not owners)
    name = parties[0].name if len(parties) == 1 else legacy[0].full_name if len(legacy) == 1 else ''
    return (next(iter(owners)) if len(owners) == 1 else None), ambiguous, name


def contact(db, body):
    number = normalize(body.get('phone'))
    if not number:
        raise HTTPException(422, 'Нужен один корректный номер телефона')
    owner_id, ambiguous, name = crm_owner(db, number)
    response = {'contact_name': name[:255] if not ambiguous else ''}
    config = locked_config(db)
    # Missing diversion is allowed only with explicit confirmation that the
    # provider CRM scenario is scoped to this main number.
    diversion = normalize(body.get('diversion'))
    if not config.enabled or not config.pbx_setup_confirmed or (body.get('diversion') and diversion != config.main_phone):
        db.commit()
        return response
    members = [m for m in db.query(PhoneRoutingMember).filter_by(enabled=True).order_by(PhoneRoutingMember.user_id)
               if eligible(db.get(User, m.user_id))]
    if ambiguous or not members:
        db.commit()
        return response
    memory = db.get(PhoneRoutingMemory, number)
    if owner_id:
        if db.get(User, owner_id) is None:
            # Historical CRM imports may reference an unmapped external owner.
            db.commit()
            return response
        if memory is None:
            memory = PhoneRoutingMemory(phone=number, user_id=owner_id, source='crm', confirmed=True)
            db.add(memory)
        else:
            memory.user_id, memory.source, memory.confirmed = owner_id, 'crm', True
    by_user = {m.user_id: m for m in members}
    if memory:
        member = by_user.get(memory.user_id)
        # An absent owner goes through the PBX sales queue, without reassignment.
        if member:
            response['responsible'] = member.pbx_user
    else:
        member = members[config.cursor % len(members)]
        config.cursor += 1
        db.add(PhoneRoutingMemory(phone=number, user_id=member.user_id, source='assigned', confirmed=False))
        response['responsible'] = member.pbx_user
    db.commit()
    return response


def answered_member(db, row, body):
    identifiers = {str(body[key]).strip() for key in ('user', 'ext') if body.get(key)}
    if not identifiers and row.employee:
        identifiers = {str(row.employee).strip()}
    matches = []
    for member, user in db.query(PhoneRoutingMember, User).join(User, User.id == PhoneRoutingMember.user_id):
        aliases = member_identifiers(member, user)
        if identifiers.intersection(aliases):
            matches.append((member, user, aliases))
    # A profile can be edited after the routing configuration was saved.
    # Ambiguous aliases (including disabled users) must never assign a client.
    if len(matches) != 1:
        return None
    member, user, aliases = matches[0]
    return member if identifiers.issubset(aliases) and member.enabled and eligible(user) else None


def observe_answer(db, row, body):
    # Only confirm a live first assignment. Historical replay cannot create or
    # replace ownership, and answering an overflow call never steals a client.
    answered = (body.get('cmd') == 'event' and body.get('type') == 'ACCEPTED')
    if not answered or row.direction != 'in':
        return
    config = locked_config(db)
    if not config.enabled:
        return
    diversion = normalize(body.get('diversion'))
    if body.get('diversion') and diversion != config.main_phone:
        return
    memory = db.get(PhoneRoutingMemory, row.phone)
    if memory and (memory.confirmed or memory.source != 'assigned'):
        return
    member = answered_member(db, row, body)
    if member:
        if memory is None:
            db.add(PhoneRoutingMemory(phone=row.phone, user_id=member.user_id, source='answered', confirmed=True))
        else:
            # Overflow changes who answered, never the assigned owner.
            memory.confirmed = True
