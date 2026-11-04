"""Serialize MAX binding creation across CRM, client and partner tables."""
from fastapi import HTTPException
from sqlalchemy import text


def lock_chat(db, chat_id):
    if db.get_bind().dialect.name == 'postgresql':
        db.execute(text('SELECT pg_advisory_xact_lock(:chat_id)'), {'chat_id': chat_id})


def guard_legacy_link(db, chat_id):
    from app.crm.chat_models import CRMChatLink
    lock_chat(db, chat_id)
    if db.query(CRMChatLink.id).filter(CRMChatLink.max_chat_id == chat_id).first():
        raise HTTPException(409, 'Чат уже связан с карточкой CRM. Откройте её из списка чатов.')
