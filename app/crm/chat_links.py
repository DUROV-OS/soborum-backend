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
from app.crm.models import CRMEntity
from app.crm.schemas import Entry
from app.max.chat_link_guard import lock_chat


class CreateCard(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['contact', 'company']
    name: str = Field(min_length=1, max_length=1000)
    phone: str = Field('', max_length=100)
    email: str = Field('', max_length=255)


class LinkFromMax(BaseModel):
    model_config = ConfigDict(extra='forbid')
    max_chat_id: int = Field(ge=-9007199254740991, le=9007199254740991)
    entity_id: int | None = Field(None, ge=1)
    create: CreateCard | None = None
    label: str = Field('MAX', min_length=1, max_length=255)

    @model_validator(mode='after')
    def selected_target(self):
        if self.max_chat_id == 0:
            raise ValueError('Избранное не является клиентским диалогом')
        if (self.entity_id is None) == (self.create is None):
            raise ValueError('Выберите существующую карточку или создание новой')
        if not self.label.strip() or (self.create and not self.create.name.strip()):
            raise ValueError('Введите название')
        return self


def out(db, link):
    row = service.get(db, link.entity_id)
    return {'id': link.id, 'entity_id': link.entity_id, 'max_chat_id': link.max_chat_id,
            'label': link.label, 'created_at': link.created_at, 'entity_kind': row.kind,
            'entity_name': row.name}


def list_links(db, entity_id):
    service.get(db, entity_id)
    return [out(db, link) for link in db.query(CRMChatLink).filter_by(entity_id=entity_id).order_by(CRMChatLink.id)]


def create_link(db, payload, user):
    # The normal CRM create service commits its Session. Bind a separate Session
    # to the request's outer transaction so its commits release savepoints only.
    # On sqlite's legacy transaction mode SAVEPOINT must have an explicit BEGIN.
    connection = db.connection()
    if connection.dialect.name == 'sqlite' and not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql('BEGIN')
    try:
        with Session(bind=connection, join_transaction_mode='create_savepoint', autoflush=False) as work:
            lock_chat(work, payload.max_chat_id)
            existing = work.query(CRMChatLink).filter_by(max_chat_id=payload.max_chat_id).first()
            if existing:
                entity = service.get(work, existing.entity_id)
                same_target = payload.entity_id == existing.entity_id if payload.entity_id else payload.create.kind == entity.kind
                if not same_target:
                    raise HTTPException(409, 'Чат уже связан с другой карточкой CRM')
                result = out(work, existing)
            else:
                if work.query(ClientChatLink.id).filter_by(max_chat_id=payload.max_chat_id).first() or work.query(PartnerChatLink.id).filter_by(max_chat_id=payload.max_chat_id).first():
                    raise HTTPException(409, 'Чат уже связан с клиентом или партнёром. Текущая связь сохранена.')
                if payload.create:
                    card = payload.create
                    created = service.create(work, card.kind, Entry(name=card.name, data={
                        'phone': card.phone, 'email': card.email, 'responsible_user_id': user.id,
                    }), user)
                    entity = service.get(work, created['id'])
                else:
                    entity = service.get(work, payload.entity_id)
                if entity.kind not in ('contact', 'company') or entity.archived:
                    raise HTTPException(422, 'Выберите действующий контакт или компанию CRM')
                link = CRMChatLink(entity_id=entity.id, max_chat_id=payload.max_chat_id,
                                   label=payload.label.strip(), created_by_id=user.id)
                work.add(link)
                work.flush()
                result = out(work, link)
            work.commit()
        db.commit()
        return result
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(409, 'Связь уже изменена. Обновите список чатов.') from error
    except BaseException:
        db.rollback()
        raise
