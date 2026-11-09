from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session
from app.common.module_access import Module
from app.core.deps import require_view, require_edit
from app.db.session import get_db
from app.users.models import User
from app.crm import communications
from app.max import service as max_service

router = APIRouter()
view, edit = require_view(Module.CLIENTS), require_edit(Module.CLIENTS)


class ChatLink(BaseModel):
    model_config = ConfigDict(extra='forbid')
    chat_id: int = Field(ge=-9007199254740991, le=9007199254740991)
    label: str = Field(min_length=1, max_length=255)

    @field_validator('chat_id')
    @classmethod
    def not_self(cls, value):
        if not value:
            raise ValueError('Выберите клиентский чат')
        return value


class Message(BaseModel):
    model_config = ConfigDict(extra='forbid')
    text: str = Field(min_length=1, max_length=4000)

    @field_validator('text')
    @classmethod
    def not_blank(cls, value):
        if not value.strip():
            raise ValueError('Введите сообщение')
        return value.strip()


@router.get('/entries/{entity_id}/chats')
def links(entity_id: int, db: Session = Depends(get_db), user: User = Depends(view)):
    return communications.links(db, entity_id)


@router.post('/entries/{entity_id}/chats')
def attach(entity_id: int, body: ChatLink, db: Session = Depends(get_db), user: User = Depends(edit)):
    return communications.attach(db, entity_id, body.chat_id, body.label, user)


@router.delete('/entries/{entity_id}/chats/{link_id}')
def detach(entity_id: int, link_id: int, db: Session = Depends(get_db), user: User = Depends(edit)):
    return communications.detach(db, entity_id, link_id, user)


@router.get('/entries/{entity_id}/chats/{chat_id}/messages')
def history(entity_id: int, chat_id: int, limit: int = Query(30, ge=1, le=100),
            db: Session = Depends(get_db), user: User = Depends(view)):
    communications.require_chat(db, entity_id, chat_id)
    return max_service.get_chat(chat_id, limit=limit)


@router.post('/entries/{entity_id}/chats/{chat_id}/messages', status_code=201)
def send(entity_id: int, chat_id: int, body: Message,
         db: Session = Depends(get_db), user: User = Depends(edit)):
    communications.require_chat(db, entity_id, chat_id, editing=True)
    # A single explicit click sends once. The caller retains its draft if the
    # remote outcome is uncertain; no transport/application retries.
    try:
        result = max_service.send_message(chat_id, body.text)
        if not isinstance(result, dict) or not isinstance(result.get('message'), dict) or not result['message'].get('id'):
            raise HTTPException(502, 'MAX не подтвердил отправку сообщения')
        return result
    except HTTPException as error:
        if error.status_code < 500:
            raise
        raise HTTPException(502, 'Не удалось подтвердить отправку в MAX. Проверьте переписку перед повтором.') from error
    except Exception as error:
        raise HTTPException(502, 'Не удалось подтвердить отправку в MAX. Проверьте переписку перед повтором.') from error
