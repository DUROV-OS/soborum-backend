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
    source_id = 'phone/megafon/' + row.phone
    # Include archived cards in deduplication. A repeated call cannot resurrect a lead.
    if db.query(CRMEntity).filter_by(kind='deal', source_id=source_id).first():
        return
    entries = db.query(CRMEntity).filter(CRMEntity.kind.in_(['contact', 'company', 'inquiry'])).all()
    if any(row.phone in routing.numbers(entry) for entry in entries):
        return
    if any(routing.normalize(client.phone) == row.phone for client in db.query(Client)):
        return
    pipeline, stage = target
    responsible = owner(db, config, row.phone, row, body)
    name = 'Звонок: +' + row.phone
    contact = CRMEntity(kind='contact', source_id=source_id, name=name, archived=False,
        payload=ContactData(phone='+' + row.phone, responsible_user_id=responsible).model_dump(mode='json'))
    db.add(contact)
    db.flush()
    deal = CRMEntity(kind='deal', source_id=source_id, name=name, archived=False,
        payload=DealData(pipeline_id=pipeline.id, stage_id=stage.id, contact_ids=[contact.id],
                         primary_contact_id=contact.id, responsible_user_id=responsible,
                         is_unsorted=False).model_dump(mode='json'))
    db.add(deal)
    db.flush()
    db.add(CRMEvent(entity_id=deal.id, actor_id=config.lead_actor_id, action='phone_lead_created', details={
        'text': 'Заявка создана автоматически по входящему звонку на общий номер компании.',
        'provider': 'megafon', 'call_id': body.get('callid') or None,
        'contact_id': contact.id, 'stage_id': stage.id,
    }))
    ensure_followup(db, deal, key='followup/' + source_id, responsible_id=responsible)
