from app.costing import governance
from app.common.files import file_integrity
from .models import Details, Change
from . import service as s


def basis(db,cp):
    data=s.card(db,cp)
    profile=db.get(Details,cp.id)
    return s.digest({'version':profile.version if profile else 0,'name':data['name'],'inn':data['inn'],'accounts':[r for r in data['accounts'] if not r['archived']]})


def state(db,cp,user=None):
    person=governance.responsible(db,'accounting')
    result={'verified':False,'can_verify':bool(user and person and user.id==person.id),'reviewer':person.full_name if person else None}
    rows=db.query(Change).filter_by(counterparty_id=cp.id).order_by(Change.id.desc())
    event=next((r for r in rows if r.data.get('kind')=='bank_verification'),None)
    if not event or not person or event.actor_id!=person.id or event.data['basis']!=basis(db,cp) or not cp.supplier_id:return result
    from app.purchasing.controls import documents
    docs=documents(db,cp.supplier_id)
    result['verified']=bool(event.data['proofs']) and all(int(id) in docs and docs[int(id)].sha256==proof['sha256'] and docs[int(id)].size_bytes==proof['size_bytes'] and file_integrity(docs[int(id)])=='matches' for id,proof in event.data['proofs'].items())
    return result


def ready_for_supplier(db,supplier_id):
    from app.accounting.models import Counterparty
    rows=db.query(Counterparty).filter_by(supplier_id=supplier_id).all()
    if not any(db.get(Details,r.id) for r in rows):return True  # existing policy remains unchanged until requisites are introduced
    return len(rows)==1 and state(db,rows[0])['verified']
