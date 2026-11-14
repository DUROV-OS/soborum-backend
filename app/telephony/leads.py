"""Create one sales card per new caller, only on the configured inbound line."""
from app.clients.models import Client
from app.crm.models import CRMEntity, CRMEvent
from app.crm.schemas import ContactData, DealData
from app.crm.guard import ensure_followup
from app.telephony import routing
from app.telephony.models import PhoneRoutingMember, PhoneRoutingMemory
from app.users.models import User


def destination(db, stage_id):
    stage = db.get(CRMEntity, stage_id) if stage_id else None
    if not stage or stage.kind != 'stage' or stage.archived or stage.payload.get('outcome', 'open') != 'open':
        return None
    pipeline = db.get(CRMEntity, stage.payload.get('pipeline_id'))
    if not pipeline or pipeline.kind != 'pipeline' or pipeline.archived or pipeline.payload.get('purpose') == 'agency':
        return None
    return pipeline, stage


def owner(db, config, number, row, body):
    memory = db.get(PhoneRoutingMemory, number) if number else None
    if memory:
        # Never replace an earlier owner because somebody else answered overflow.
        return memory.user_id if routing.eligible(db.get(User, memory.user_id)) else None
    members = [m for m in db.query(PhoneRoutingMember).filter_by(enabled=True).order_by(PhoneRoutingMember.user_id)
               if routing.eligible(db.get(User, m.user_id))]
    if not members:
        return None
    answered = body.get('type') == 'ACCEPTED' or (body.get('cmd') == 'history' and body.get('status') == 'Success')
    member = routing.answered_member(db, row, body) if answered else None
    if member is None:
        member = members[config.cursor % len(members)]
        config.cursor += 1
    if number:
        db.add(PhoneRoutingMemory(phone=number, user_id=member.user_id,
                                 source='answered' if answered else 'assigned', confirmed=answered))
    return member.user_id


def ensure(db, row, body):
    """Caller owns the transaction. Contact lookups and archive imports do not enter here."""
    if row.direction != 'in' or not row.phone:
        return
    if body.get('cmd') not in ('event', 'history') or body.get('type') == 'OUTGOING':
        return
    config = routing.locked_config(db)
    # No inferred DID: direct staff numbers and unidentifiable lines cannot create leads.
    if not config.auto_create_leads or routing.normalize(body.get('diversion')) != config.main_phone:
        return
    target = destination(db, config.lead_stage_id)
    if not target or not config.lead_actor_id or not db.get(User, config.lead_actor_id):
        return
    if getattr(row, 'deal_id', None) and getattr(row, 'deal_match_status', '') in ('manual', 'inquiry', 'current'):
        return
    source_id = 'phone/megafon/' + row.phone
    entries = [entry for entry in db.query(CRMEntity).filter(
        CRMEntity.kind.in_(['contact', 'company', 'inquiry']), CRMEntity.archived.is_(False)
    ) if row.phone in routing.numbers(entry)]
    clients = [client for client in db.query(Client) if routing.normalize(client.phone) == row.phone]
    from app.telephony.service import deal_contexts, same_customer_records
    # Explicit company/contact links are one customer; unrelated shared numbers stay manual.
    if len(clients) > 1 or (len(entries) > 1 and not same_customer_records(db, entries)):
        return
    entry_ids, client_ids = {entry.id for entry in entries}, {client.id for client in clients}
    candidates = []
    related = []
    for deal, ids, numbers, client_id in deal_contexts(db):
        if not (row.phone in numbers or ids.intersection(entry_ids) or client_id in client_ids):
            continue
        related.append(deal)
        stage = db.get(CRMEntity, deal.payload.get('stage_id'))
        pipeline = db.get(CRMEntity, deal.payload.get('pipeline_id'))
        if (stage and not stage.archived and stage.payload.get('outcome', 'open') == 'open'
                and pipeline and not pipeline.archived and pipeline.payload.get('purpose') != 'agency'):
            candidates.append(deal)
    if len(candidates) == 1:
        row.deal_id, row.deal_match_status = candidates[0].id, 'current'
        return
    if candidates:
        return
    previous = db.query(CRMEntity).filter_by(kind='deal', source_id=source_id).first()
    # An archived automatic card remains archived; old archive imports never enter here.
    if previous and previous.archived:
        return
    if entries and entries[0].kind == 'inquiry':
        return
    if previous:
        source_id += '/after/' + str(max([deal.id for deal in related] + [previous.id]))
        if db.query(CRMEntity).filter_by(kind='deal', source_id=source_id).first():
            return
    pipeline, stage = target
    responsible = entries[0].payload.get('responsible_user_id') if entries else clients[0].manager_id if clients else None
    if not responsible or not routing.eligible(db.get(User, responsible)):
        responsible = owner(db, config, row.phone, row, body)
    name = 'Звонок: +' + row.phone
    contact = next((entry for entry in entries if entry.kind == 'contact'), None)
    company = next((entry for entry in entries if entry.kind == 'company'), None)
    if contact is None and company is None:
        contact_source = 'phone/megafon/' + row.phone
        contact = db.query(CRMEntity).filter_by(kind='contact', source_id=contact_source).first()
        if contact is not None and contact.archived:
            return
        if contact is None:
            contact = CRMEntity(kind='contact', source_id=contact_source, name=name, archived=False,
                payload=ContactData(phone='+' + row.phone, responsible_user_id=responsible).model_dump(mode='json'))
            db.add(contact)
            db.flush()
    deal = CRMEntity(kind='deal', source_id=source_id, name=name, archived=False,
        payload=DealData(pipeline_id=pipeline.id, stage_id=stage.id,
                         contact_ids=[contact.id] if contact else [], primary_contact_id=contact.id if contact else None,
                         company_id=company.id if company else None, client_id=clients[0].id if clients else None,
                         responsible_user_id=responsible,
                         is_unsorted=False).model_dump(mode='json'))
    db.add(deal)
    db.flush()
    db.add(CRMEvent(entity_id=deal.id, actor_id=config.lead_actor_id, action='phone_lead_created', details={
        'text': 'Заявка создана автоматически по входящему звонку на общий номер компании.',
        'provider': 'megafon', 'call_id': body.get('callid') or None,
        'contact_id': contact.id if contact else None, 'stage_id': stage.id,
    }))
    ensure_followup(db, deal, key='followup/' + source_id, responsible_id=responsible)
    row.deal_id, row.deal_match_status = deal.id, 'current'
