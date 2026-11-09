"""Local CRM binding only: never logs into MAX or sends a message."""
from typing import Literal
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from app.clients.models import ClientChatLink
from app.partners.models import PartnerChatLink
from app.crm import service
from app.crm.chat_models import CRMChatLink
from app.crm.models import CRMEntity, CRMEvent, CRMTaskLink
from app.crm.schemas import Entry
from app.max.chat_link_guard import lock_chat


class CreateCard(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['contact', 'company', 'deal']
    name: str = Field(min_length=1, max_length=1000)
    phone: str = Field('', max_length=100)
    email: str = Field('', max_length=255)


class LinkFromMax(BaseModel):
    model_config = ConfigDict(extra='forbid')
    max_chat_id: int = Field(ge=-9007199254740991, le=9007199254740991)
    entity_id: int | None = Field(None, ge=1)
    create: CreateCard | None = None
    label: str = Field('MAX', min_length=1, max_length=255)
    move_from_id: int | None = Field(None, ge=1)
    move_from_version: int | None = Field(None, ge=1)

    @model_validator(mode='after')
    def selected_target(self):
        if self.max_chat_id == 0:
            raise ValueError('Избранное не является клиентским диалогом')
        if (self.entity_id is None) == (self.create is None):
            raise ValueError('Выберите существующую карточку или создание новой')
        if not self.label.strip() or (self.create and not self.create.name.strip()):
            raise ValueError('Введите название')
        if (self.move_from_id is None) != (self.move_from_version is None) or (self.move_from_id and not self.entity_id):
            raise ValueError('Для переноса выберите исходную и целевую карточки')
        return self


def out(db, link):
    row = service.get(db, link.entity_id)
    return {'id': link.id, 'entity_id': link.entity_id, 'max_chat_id': link.max_chat_id,
            'label': link.label, 'created_at': link.created_at, 'entity_kind': row.kind,
            'entity_name': row.name}


def list_links(db, entity_id):
    service.get(db, entity_id)
    return [out(db, link) for link in db.query(CRMChatLink).filter_by(entity_id=entity_id).order_by(CRMChatLink.id)]


def current_link(db, chat_id):
    link = db.query(CRMChatLink).filter_by(max_chat_id=chat_id).first()
    if not link:
        return None
    row = service.get(db, link.entity_id)
    return {**out(db, link), 'entity_version': row.version,
            'automatic_lead': row.kind == 'deal' and (row.source_id or '').startswith('max/')}


def _create_lead(db, card, chat_id, user, config):
    from app.telephony import leads, routing
    from app.crm.schemas import ContactData, DealData
    from app.crm.guard import ensure_followup
    target = leads.destination(db, config.lead_stage_id)
    if not target:
        raise HTTPException(422, 'Руководителю нужно выбрать этап «Новые заявки» в настройках связи')
    pipeline, stage = target
    number = routing.normalize(card.phone)
    if card.phone.strip() and not number:
        raise HTTPException(422, 'Проверьте номер телефона клиента')
    contacts = [r for r in db.query(CRMEntity).filter_by(kind='contact', archived=False) if number and number in routing.numbers(r)]
    source = 'max/manual/' + str(chat_id)
    contact = contacts[0] if len(contacts) == 1 else None
    if contact is None:
        contact = CRMEntity(kind='contact', source_id=source, name=card.name.strip(),
            payload=ContactData(phone='+' + number if number else '', email=card.email, responsible_user_id=user.id).model_dump(mode='json'))
        db.add(contact); db.flush()
    entity = CRMEntity(kind='deal', source_id=source, name='MAX: ' + card.name.strip(),
        payload=DealData(pipeline_id=pipeline.id, stage_id=stage.id, contact_ids=[contact.id], primary_contact_id=contact.id,
                         responsible_user_id=contact.payload.get('responsible_user_id') or user.id, is_unsorted=False).model_dump(mode='json'))
    db.add(entity); db.flush()
    ensure_followup(db, entity, key='followup/' + source, responsible_id=entity.payload['responsible_user_id'])
    return entity


def _move(db, link, payload, user):
    rows = db.query(CRMEntity).filter(CRMEntity.id.in_([link.entity_id, payload.entity_id])).order_by(CRMEntity.id).with_for_update().populate_existing().all()
    by_id = {row.id: row for row in rows}
    source, target = by_id.get(link.entity_id), by_id.get(payload.entity_id)
    if not source or not target or source.id != payload.move_from_id or source.version != payload.move_from_version:
        raise HTTPException(409, 'Привязка или карточка изменились. Обновите данные перед переносом.')
    if target.kind not in ('contact', 'company', 'deal', 'inquiry') or target.archived:
        raise HTTPException(422, 'Выберите действующую карточку клиента или заявки')
    from app.telephony.models import MAXInboundMessage, PhoneCall
    from app.tasks.models import Task, TaskStatus, TaskReport
    tasks = db.query(CRMEntity).filter(CRMEntity.kind == 'task', CRMEntity.payload['entity_id'].as_integer() == source.id).order_by(CRMEntity.id).with_for_update().all()
    pristine = (source.kind == 'deal' and (source.source_id or '').startswith('max/') and source.version == 1
                and not source.archived and not source.payload.get('amount')
                and db.query(CRMChatLink).filter_by(entity_id=source.id).count() == 1
                and not db.query(PhoneCall).filter_by(deal_id=source.id).first()
                and not db.query(CRMEvent).filter(CRMEvent.entity_id == source.id, CRMEvent.action != 'max_message_received').first())
    for task in tasks:
        bridge = db.get(CRMTaskLink, task.id)
        board = db.query(Task).filter_by(id=bridge.task_id).with_for_update().first() if bridge else None
        if (task.version != 1 or not (task.source_id or '').startswith('followup/max/')
                or task.payload.get('completed') or task.payload.get('result')
                or (board and (board.status != TaskStatus.READY or db.query(TaskReport).filter_by(task_id=board.id).first()))):
            pristine = False
    link.entity_id = target.id
    db.query(MAXInboundMessage).filter_by(chat_id=str(link.max_chat_id), entity_id=source.id).update({'entity_id': target.id}, synchronize_session=False)
    for event in db.query(CRMEvent).filter(CRMEvent.entity_id == source.id, CRMEvent.action.in_(['max_message_received', 'max_message_sent']),
                                         CRMEvent.details['chat_id'].as_integer() == link.max_chat_id):
        event.entity_id = target.id
    if pristine:
        source.archived = True
        source.version += 1
        from app.crm.tasks import sync_to_board
        for task in tasks:
            task.payload = {**task.payload, 'entity_id': target.id,
                            'responsible_user_id': target.payload.get('responsible_user_id') or task.payload.get('responsible_user_id')}
            task.version += 1
            sync_to_board(db, task)
    for row in (source, target):
        db.add(CRMEvent(entity_id=row.id, actor_id=user.id, action='chat_relinked', details={
            'text': 'Переписка MAX перенесена в карточку «' + target.name + '».', 'chat_id': link.max_chat_id,
            'from_entity_id': source.id, 'to_entity_id': target.id, 'source_archived': pristine,
        }))
    db.flush()
    return {**out(db, link), 'source_archived': pristine}


def create_link(db, payload, user):
    # The normal CRM create service commits its Session. Bind a separate Session
    # to the request's outer transaction so its commits release savepoints only.
    # On sqlite's legacy transaction mode SAVEPOINT must have an explicit BEGIN.
    connection = db.connection()
    if connection.dialect.name == 'sqlite' and not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql('BEGIN')
    try:
        with Session(bind=connection, join_transaction_mode='create_savepoint', autoflush=False) as work:
            config = None
            if payload.create and payload.create.kind == 'deal':
                from app.telephony.routing import locked_config
                config = locked_config(work)
            lock_chat(work, payload.max_chat_id)
            existing = work.query(CRMChatLink).filter_by(max_chat_id=payload.max_chat_id).first()
            if existing:
                entity = service.get(work, existing.entity_id)
                same_target = payload.entity_id == existing.entity_id if payload.entity_id else payload.create.kind == entity.kind
                if not same_target and payload.move_from_id:
                    result = _move(work, existing, payload, user)
                elif not same_target:
                    raise HTTPException(409, 'Чат уже связан с другой карточкой CRM')
                else:
                    result = out(work, existing)
            else:
                if work.query(ClientChatLink.id).filter_by(max_chat_id=payload.max_chat_id).first() or work.query(PartnerChatLink.id).filter_by(max_chat_id=payload.max_chat_id).first():
                    raise HTTPException(409, 'Чат уже связан с клиентом или партнёром. Текущая связь сохранена.')
                if payload.create:
                    card = payload.create
                    if card.kind == 'deal':
                        entity = _create_lead(work, card, payload.max_chat_id, user, config)
                    else:
                        created = service.create(work, card.kind, Entry(name=card.name, data={
                            'phone': card.phone, 'email': card.email, 'responsible_user_id': user.id,
                        }), user)
                        entity = service.get(work, created['id'])
                else:
                    entity = service.get(work, payload.entity_id)
                if payload.move_from_id:
                    raise HTTPException(409, 'Исходная привязка изменилась. Обновите данные.')
                if entity.kind not in ('contact', 'company', 'deal', 'inquiry') or entity.archived:
                    raise HTTPException(422, 'Выберите действующую карточку клиента или заявки CRM')
                link = CRMChatLink(entity_id=entity.id, max_chat_id=payload.max_chat_id,
                                   label=payload.label.strip(), created_by_id=user.id)
                work.add(link)
                work.flush()
                result = out(work, link)
            from app.crm.max_intake import attach_pending
            attach_pending(work, payload.max_chat_id, result['entity_id'], user.id)
            work.commit()
        db.commit()
        return result
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(409, 'Связь уже изменена. Обновите список чатов.') from error
    except BaseException:
        db.rollback()
        raise
