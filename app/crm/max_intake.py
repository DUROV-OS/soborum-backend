"""Unlinked personal MAX messages wait for an explicit CRM card selection."""
from datetime import datetime, timedelta, timezone
from app.clients.models import Client, ClientChatLink
from app.crm.models import CRMEntity, CRMEvent
from app.crm.chat_models import CRMChatLink
from app.max.chat_link_guard import lock_chat
from app.max.service import _contact_name, _dialog_peer_id
from app.partners.models import Partner, PartnerChatLink
from app.telephony import routing
from app.telephony.models import PhoneRoutingConfig, MAXInboundMessage
from app.users.models import User


def enabled(db, message=None):
    config = db.get(PhoneRoutingConfig, 1)
    return accepts(config, message) if message is not None else bool(config and config.max_incoming_enabled)


def message_time(message):
    try:
        value = float(message.get('time') or 0)
        if value > 10**11:
            value /= 1000
        return datetime.fromtimestamp(value, timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def prepare(session, chat_id, message):
    """Only provider metadata can identify a peer. Message text is never a phone lookup."""
    viewer = str(session.viewer_id())
    sender = str(message.get('sender') or '')
    if not sender.isdigit() or sender == viewer or _dialog_peer_id(chat_id, viewer) != sender:
        return None
    meta = next((c for c in session.chats() if c.get('id') == chat_id), None)
    if not meta:
        meta = next((c for c in session.chat_info([chat_id]) if c.get('id') == chat_id), None)
    if not meta or meta.get('type') != 'DIALOG':
        return None
    contact = session.contacts_by_id().get(sender)
    if not contact:
        contact = next((c for c in session.contacts_info([int(sender)]) if str(c.get('id')) == sender), {})
    return {'account_id': viewer, 'chat_id': chat_id, 'sender_id': sender,
            'phone': routing.normalize(contact.get('phone')), 'name': (_contact_name(contact) or 'Клиент MAX')[:255]}


def _existing(db, number):
    if not number:
        return None, None, False
    cards = [r for r in db.query(CRMEntity).filter(CRMEntity.kind.in_(['contact', 'company', 'inquiry']))
             if number in routing.numbers(r)]
    clients = [r for r in db.query(Client) if routing.normalize(r.phone) == number]
    if len(cards) > 1 or len(clients) > 1:
        return None, None, True
    ids = {r.id for r in cards}
    client_id = clients[0].id if clients else None
    deals = [r for r in db.query(CRMEntity).filter_by(kind='deal', archived=False)
             if ids.intersection(r.payload.get('contact_ids') or []) or r.payload.get('company_id') in ids
             or (client_id and r.payload.get('client_id') == client_id)]
    # Prefer the single open application, while multiple deals share the contact's chat.
    open_deals = [r for r in deals if (stage := db.get(CRMEntity, r.payload.get('stage_id')))
                  and stage.payload.get('outcome', 'open') == 'open']
    if len(open_deals) == 1:
        return open_deals[0], None, False
    if len(deals) == 1:
        return deals[0], None, False
    if cards:
        entry = cards[0]
        if entry.kind == 'inquiry' and entry.payload.get('accepted_deal_id'):
            entry = db.get(CRMEntity, entry.payload['accepted_deal_id']) or entry
        return entry, None, False
    return None, clients[0] if clients else None, bool(deals)


def _history(db, receipt, entity_id, actor_id):
    receipt.entity_id, receipt.state = entity_id, 'linked'
    db.add(CRMEvent(entity_id=entity_id, actor_id=actor_id, action='max_message_received', details={
        'text': 'MAX: ' + receipt.message_text, 'chat_id': int(receipt.chat_id),
        'message_id': receipt.message_id, 'received_at': receipt.sent_at.isoformat() if receipt.sent_at else None,
        'automatic': True,
    }))


def attach_pending(db, chat_id, entity_id, actor_id):
    # Called under the same chat lock as binding creation. History moves once.
    for receipt in db.query(MAXInboundMessage).filter_by(chat_id=str(chat_id), state='needs_link').order_by(MAXInboundMessage.id):
        _history(db, receipt, entity_id, actor_id)
    from app.notifications.models import Notification
    db.query(Notification).filter_by(object_type='max_unlinked_chat', object_id=chat_id, read_at=None).update(
        {'read_at': datetime.now(timezone.utc)}, synchronize_session=False)


def pending(db, chat_id):
    receipt = db.query(MAXInboundMessage).filter_by(chat_id=str(chat_id), state='needs_link').order_by(MAXInboundMessage.id.desc()).first()
    if not receipt:
        return None
    entry, _, ambiguous = _existing(db, receipt.phone)
    return {'name': receipt.sender_name, 'phone': '+' + receipt.phone if receipt.phone else '',
            'suggested': {'id': entry.id, 'name': entry.name, 'kind': entry.kind} if entry and not ambiguous and not entry.archived else None}


def accepts(config, message):
    at, started = message_time(message), config.max_leads_started_at if config else None
    if started and started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return bool(config and config.max_incoming_enabled and started and at
                and started <= at <= datetime.now(timezone.utc) + timedelta(minutes=5))


def ingest(db, identity, message):
    if not identity or not message.get('id'):
        return None
    config = routing.locked_config(db)
    if not accepts(config, message) or not config.lead_actor_id:
        return None
    chat_id, number = identity['chat_id'], identity['phone']
    lock_chat(db, chat_id)
    key = dict(account_id=identity['account_id'], chat_id=str(chat_id), message_id=str(message['id']))
    old = db.query(MAXInboundMessage).filter_by(**key).first()
    if old:
        return old
    if number and any(routing.normalize(u.work_phone) == number for u in db.query(User)):
        return None
    if db.query(PartnerChatLink).filter_by(max_chat_id=chat_id).first():
        return None
    if number and any(routing.normalize(p.phone) == number for p in db.query(Partner)):
        return None
    existing_pending = db.query(MAXInboundMessage.id).filter_by(account_id=identity['account_id'], chat_id=str(chat_id), state='needs_link').first()
    receipt = MAXInboundMessage(**key, state='needs_link', phone=number, sender_name=identity['name'],
        sent_at=message_time(message), message_text=str(message.get('text') or ('Вложение' if message.get('attaches') else 'Сообщение'))[:4000])
    db.add(receipt)
    link = db.query(CRMChatLink).filter_by(max_chat_id=chat_id).first()
    legacy_link = db.query(ClientChatLink).filter_by(max_chat_id=chat_id).first()
    if link:
        _history(db, receipt, link.entity_id, config.lead_actor_id)
    elif legacy_link:
        receipt.state = 'legacy_linked'
    elif not existing_pending:
        # Do not call notify(), which commits halfway through its caller's transaction.
        # Notifications become visible atomically with the pending conversation.
        from app.notifications.models import Notification, NotificationKind
        from app.notifications.service import is_muted
        from app.common.module_access import Module
        for user in db.query(User).filter(User.is_active.is_(True), User.job_role.in_(['manager', 'director'])):
            if routing.eligible(user) and not is_muted(db, user.id, Module.CLIENTS, chat_id):
                db.add(Notification(user_id=user.id, kind=NotificationKind.MAX_MESSAGE,
                    title='Новый клиент написал в MAX', body=identity['name'] + '. Создайте новый лид или привяжите диалог к существующему клиенту.',
                    object_type='max_unlinked_chat', object_id=chat_id))
    db.flush()
    return receipt
