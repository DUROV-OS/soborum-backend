"""Explicit MAX links for local CRM cards. No phone-based message routing."""
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from app.clients.models import ClientChatLink
from app.crm.models import CRMEntity, CRMEvent
from app.crm.chat_models import CRMChatLink
from app.max.chat_link_guard import lock_chat
from app.partners.models import PartnerChatLink


def card(db, entity_id, *, editing=False):
    entry = db.get(CRMEntity, entity_id)
    if not entry or entry.kind not in ('deal', 'contact', 'company', 'inquiry'):
        raise HTTPException(404, 'Карточка клиента или заявки не найдена')
    if editing and entry.archived:
        raise HTTPException(409, 'Архивная карточка доступна только для чтения')
    return entry


def links(db, entity_id):
    entry = card(db, entity_id)
    ids = {entry.id}
    if entry.kind == 'inquiry' and entry.payload.get('accepted_deal_id'):
        ids.add(entry.payload['accepted_deal_id'])
    if entry.kind == 'deal':
        ids.update(entry.payload.get('contact_ids') or [])
        if entry.payload.get('company_id'):
            ids.add(entry.payload['company_id'])
    result = []
    for link in db.query(CRMChatLink).filter(CRMChatLink.entity_id.in_(ids)).order_by(CRMChatLink.id):
        result.append({'key': f'crm:{link.id}', 'chat_id': link.max_chat_id, 'label': link.label,
                       'owner_id': link.entity_id, 'can_unlink': link.entity_id == entry.id})
    client_id = entry.payload.get('client_id')
    if client_id:
        for link in db.query(ClientChatLink).filter_by(client_id=client_id).order_by(ClientChatLink.id):
            result.append({'key': f'client:{link.id}', 'chat_id': link.max_chat_id, 'label': link.label,
                           'owner_id': None, 'can_unlink': False})
    return {'items': result}


def attach(db, entity_id, chat_id, label, user):
    card(db, entity_id, editing=True)
    lock_chat(db, chat_id)
    existing = db.query(CRMChatLink).filter_by(max_chat_id=chat_id).first()
    if existing:
        if existing.entity_id == entity_id:
            return links(db, entity_id)
        raise HTTPException(409, 'Чат уже связан с другой карточкой. Откройте её или свяжите контакт с заявкой.')
    if (db.query(ClientChatLink).filter_by(max_chat_id=chat_id).first()
            or db.query(PartnerChatLink).filter_by(max_chat_id=chat_id).first()):
        raise HTTPException(409, 'Чат уже привязан к клиенту или партнёру. Используйте существующую карточку.')
    label = label.strip()
    if not label:
        raise HTTPException(422, 'Укажите название чата')
    db.add(CRMChatLink(entity_id=entity_id, max_chat_id=chat_id, label=label, created_by_id=user.id))
    db.add(CRMEvent(entity_id=entity_id, actor_id=user.id, action='chat_linked', details={'chat_id': chat_id}))
    from app.crm.max_intake import attach_pending
    attach_pending(db, chat_id, entity_id, user.id)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, 'Чат уже привязан. Обновите карточку.')
    return links(db, entity_id)


def detach(db, entity_id, link_id, user):
    card(db, entity_id, editing=True)
    link = db.get(CRMChatLink, link_id)
    if not link or link.entity_id != entity_id:
        raise HTTPException(404, 'Привязка в этой карточке не найдена')
    lock_chat(db, link.max_chat_id)
    db.add(CRMEvent(entity_id=entity_id, actor_id=user.id, action='chat_unlinked', details={'chat_id': link.max_chat_id}))
    db.delete(link)
    db.commit()
    return links(db, entity_id)


def require_chat(db, entity_id, chat_id, *, editing=False):
    card(db, entity_id, editing=editing)
    if not any(link['chat_id'] == chat_id for link in links(db, entity_id)['items']):
        raise HTTPException(404, 'Этот чат не привязан к карточке')


def phone_choices(db, entity_id):
    from app.telephony.routing import numbers, normalize
    from app.clients.models import Client
    entry = card(db, entity_id)
    rows = [entry]
    if entry.kind == 'deal':
        ids = set(entry.payload.get('contact_ids') or [])
        if entry.payload.get('company_id'):
            ids.add(entry.payload['company_id'])
        rows += db.query(CRMEntity).filter(CRMEntity.id.in_(ids), CRMEntity.archived.is_(False)).all() if ids else []
    found = {}
    for row in rows:
        for number in sorted(numbers(row)):
            found.setdefault(number, {'phone': '+' + number, 'name': row.name})
    client = db.get(Client, entry.payload.get('client_id')) if entry.payload.get('client_id') else None
    if client and normalize(client.phone):
        number = normalize(client.phone)
        found.setdefault(number, {'phone': '+' + number, 'name': client.full_name})
    return {'items': list(found.values())}


def start_from_card(db, entity_id, phone, user):
    from app.telephony.routing import normalize
    from app.max import service as max_service
    card(db, entity_id, editing=True)
    choice = next((item for item in phone_choices(db, entity_id)['items'] if normalize(item['phone']) == normalize(phone)), None)
    if not choice:
        raise HTTPException(422, 'Сначала сохраните номер в карточке этого клиента')
    # Starting a dialog may add a contact in MAX, but never sends a message.
    result = max_service.start_dialog(choice['phone'], choice['name'][:64])
    if not any(link['chat_id'] == result['chatId'] for link in links(db, entity_id)['items']):
        attach(db, entity_id, result['chatId'], (result.get('name') or choice['name'] or 'MAX')[:255], user)
    return result
