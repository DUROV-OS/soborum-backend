"""Real signed evidence for existing stock tests; no production bypasses or mocks."""
from io import BytesIO
from uuid import uuid4
from PIL import Image
from app.common.files import save_bytes_file, FilePurpose
from app.costing.governance_models import CostingApprovalPolicy
from app.users.models import User, UserRole
from app.warehouse.control_models import WarehouseControlDocument
from app.warehouse.control_router import Handoff
from app.warehouse import controls


def evidence(db, user):
    raw=BytesIO();Image.new('RGB',(2,2),'white').save(raw,'PNG')
    f=save_bytes_file(db,'Synthetic norm.png','image/png',raw.getvalue(),FilePurpose.COSTING_EVIDENCE,user)
    db.add(WarehouseControlDocument(file_id=f.id,actor_id=user.id));db.flush();return f


def approved(db, production_id, person=None):
    policy=db.get(CostingApprovalPolicy,1)
    if person is None:
        person=User(email=str(uuid4())+'@test.invalid',full_name='Synthetic production approver',role=UserRole.ADMIN,hashed_password='no-login',is_active=True)
        db.add(person);db.flush()
    if not policy:
        policy=CostingApprovalPolicy(id=1,production_user_id=person.id,accounting_user_id=person.id,director_user_id=person.id)
        db.add(policy)
    else:policy.production_user_id=person.id
    db.flush()
    f=evidence(db,person)
    body=Handoff(request_id=uuid4(),source_hash=controls.state(db,production_id)['source_hash'],file_id=f.id,locator='Synthetic table 1, all rows')
    controls.handoff(db,production_id,body,person);db.commit();return body.source_hash,person,f


def legacy_issue_client(client,db):
    """Adapt old endpoint scenarios to explicit signature and idempotency contracts."""
    import re
    original=client.post
    def post(url,*args,**kwargs):
        tech=re.fullmatch(r'/api/warehouse/techcard-issue/(\d+)',url)
        request=re.fullmatch(r'/api/warehouse/requests/(\d+)/approve',url)
        if tech or request:
            from app.production.models import MaterialRequest
            req=db.get(MaterialRequest,int(request[1])) if request else None
            pid=int(tech[1]) if tech else req.block_material.block.production_id
            state=controls.state(db,pid)
            digest=state['source_hash'] if state['approved'] else approved(db,pid)[0]
            body=dict(kwargs.get('json') or {})
            body.update(request_id=str(uuid4()),source_hash=digest)
            if req:
                body.setdefault('received_by','Synthetic recipient');body.setdefault('quantity',float(req.quantity))
            kwargs['json']=body
        return original(url,*args,**kwargs)
    client.post=post
    return client


def inventory_act(client, doc):
    if doc.get('kind') != 'inventory': return {}
    history=client.get(f"/api/warehouse/inventory-evidence/{doc['id']}")
    if history.status_code!=200:return {}
    for act in history.json():
        if act['stage']=='prepared' and act['snapshot']['version']==doc['version']:
            return {'act_id':act['id'],'confirmed':True}
    raw=BytesIO();Image.new('RGB',(2,2),'white').save(raw,'PNG')
    upload=client.post('/api/warehouse/control/documents',files={'file':('synthetic-count.png',raw.getvalue(),'image/png')})
    if upload.status_code!=201:return {}
    result=client.post(f"/api/warehouse/inventory-evidence/{doc['id']}",json={'request_id':str(uuid4()),'version':doc['version'],'file_id':upload.json()['file_id'],'locator':'Synthetic count table, every row','confirmed':True})
    return {'act_id':result.json()['id'],'confirmed':True} if result.status_code==201 else {}
