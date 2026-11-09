from io import BytesIO
from typing import Literal
from uuid import uuid4
from fastapi import APIRouter, Depends, UploadFile
from sqlalchemy.orm import Session
from app.common.files import FileAsset, FilePurpose, save_bytes_file, verified_file_response
from app.common.module_access import AccessLevel
from app.core.deps import get_current_user
from app.db.session import get_db
from app.users.models import User, UserRole
from app.costing import service as s, governance as g
from app.costing.governance_models import CostingApprovalPolicy as Policy, CostingSignoff as Signoff, CostingOverheadPlan as Plan, CostingEvidenceDocument as Document
from app.costing.governance_schemas import Review, OverheadInput, PolicyInput
from app.costing.models import CostingItem, CostingEstimate
from app.costing.schemas import EstimateInput, Strict
from app.costing.technical_basis import fingerprint, item_basis
from pydantic import Field

router=APIRouter(prefix='/governance',tags=['Согласование расчётов'])


def readable(db,user):
    if not g.may_read(db,user): s.fail('Нет доступа к согласованию расчётов',403)


def financial(db,user,edit=False):
    if not g.financial_access(db,user) or (edit and not s.cost_access(user,AccessLevel.EDIT) and user.id!=getattr(g.responsible(db,'accounting'),'id',None)):
        s.fail('Нет доступа к финансовым основаниям',403)


def target_row(db,target,id,lock=False):
    model={'estimate':CostingEstimate,'month':Plan,'item':CostingItem}[target]
    q=db.query(model).filter_by(id=id)
    if lock: q=q.with_for_update().populate_existing()
    row=q.first()
    if not row: s.fail('Запись не найдена',404)
    return row


def document_list(db,target,id):
    result=[]
    for doc,asset in db.query(Document,FileAsset).join(FileAsset,FileAsset.id==Document.file_id).filter(Document.target==target,Document.target_id==id).order_by(Document.id):
        result.append({'id':doc.id,'title':doc.title,'filename':asset.filename,'sha256':asset.sha256,
                       'size_bytes':asset.size_bytes,'created_at':doc.created_at})
    return result


def history_list(db,target,id,financial=True):
    rows=db.query(Signoff).filter_by(target=target,target_id=id).order_by(Signoff.id).all()
    return [{'id':r.id,'stage':r.stage,'version':r.version,'actor':db.get(User,r.actor_id).full_name,
             'created_at':r.created_at,'reason':r.content['request']['reason'],'work_scope':r.content.get('work_scope',''),
             **({'evidence':r.content.get('evidence',[])} if financial else {})}
            for r in rows if financial or r.stage=='production']


@router.get('/access')
def access(db:Session=Depends(get_db),user=Depends(get_current_user)):
    readable(db,user)
    people={stage:g.responsible(db,stage) for stage in ('production','accounting','director')}
    return {'financial':g.financial_access(db,user),'edit':s.cost_access(user,AccessLevel.EDIT) or getattr(people['accounting'],'id',None)==user.id,
        'stages':[stage for stage,person in people.items() if person and person.id==user.id],
        'responsible':{stage:{'id':person.id,'name':person.full_name} if person else None for stage,person in people.items()},
        'can_assign':user.role==UserRole.ADMIN and user.job_role=='director'}


@router.put('/policy')
def save_policy(body:PolicyInput,db:Session=Depends(get_db),user=Depends(get_current_user)):
    if user.role!=UserRole.ADMIN or user.job_role!='director': s.fail('Ответственных назначает руководитель',403)
    s.lock(db)
    current=g.responsible(db,'director')
    if current and current.id!=user.id: s.fail('Ответственных меняет назначенный руководитель',403)
    for uid in body.model_dump().values():
        person=db.get(User,uid)
        if not person or not person.is_active: s.fail('Выберите действующих сотрудников')
    row=g.policy(db) or Policy(id=1)
    for key,value in body.model_dump().items(): setattr(row,key,value)
    db.add(row);db.commit();return access(db,user)


@router.get('/queue')
def queue(db:Session=Depends(get_db),user=Depends(get_current_user)):
    readable(db,user)
    source=s.sources(db)
    items=[{'id':row['id'],'version':row['version'],'title':row['title'],'kind':row['kind'],
            'production_approved':source['production_approved'].get(str(row['id']),False)}
           for row in source['items'].values() if not row['archived']]
    estimates=[]
    for row in db.query(CostingEstimate).filter_by(archived=False).order_by(CostingEstimate.id.desc()).limit(200):
        state=g.review_state(db,row,current_sources=source)
        if not g.financial_access(db,user): state={k:v for k,v in state.items() if k!='issues'}
        estimates.append({'id':row.id,'version':row.version,'title':row.title,'number':row.number,'state':state})
    return {'items':items,'estimates':estimates}


@router.get('/items/{id}')
def item(id:int,db:Session=Depends(get_db),user=Depends(get_current_user)):
    readable(db,user)
    row=target_row(db,'item',id);source=s.sources(db);out=s.item_out(row)
    basis=item_basis(out,source)
    if row.kind=='package': basis['members']=[item_basis(source['items'][str(x['addon_id'])],source) for x in row.content['addons']]
    return {'id':row.id,'version':row.version,'title':row.title,'basis':basis,'production_approved':g.item_ready(db,out,source)}


@router.post('/items/{id}/production')
def item_approval(id:int,body:Review,db:Session=Depends(get_db),user=Depends(get_current_user)):
    g.require_person(db,user,'production');s.lock(db)
    row=target_row(db,'item',id,True)
    if g.replay(db,user,'item',id,'production',body): return item(id,db,user)
    if row.archived or row.version!=body.version: s.fail('Комплектация изменилась',409)
    if len(body.work_scope.strip())<5: s.fail('Подтвердите состав работ и технологические условия')
    source=s.sources(db);basis=item_basis(s.item_out(row),source)
    used={'items':{str(row.id):s.item_out(row)}}
    if row.kind=='package':
        if not row.content['addons']: s.fail('Пакет не содержит опций')
        members=[source['items'][str(part['addon_id'])] for part in row.content['addons']]
        used['items'].update({str(member['id']):member for member in members})
        basis['members']=[item_basis(member,source) for member in members]
        if any(member['archived'] or not member['lines'] or any(line['archived'] for line in member['lines']+member['replacements']) for member in basis['members']):
            s.fail('В пакете есть неполная или архивная комплектация')
    issues=g.recipe_problems(db,used)
    if issues: s.fail('; '.join(issues),409)
    if not basis['lines'] and row.kind!='package' and not basis['service_only']: s.fail('Не заполнен состав изделия')
    if any(line['archived'] for line in basis['lines']+basis['replacements']): s.fail('В составе есть архивные материалы')
    g.record(db,user,'item',id,'production',row.version,g.item_digest(s.item_out(row),source),body,technical_basis=basis,work_scope=body.work_scope)
    db.commit();return item(id,db,user)


@router.get('/estimates/{id}')
def estimate(id:int,db:Session=Depends(get_db),user=Depends(get_current_user)):
    readable(db,user);row=target_row(db,'estimate',id);revision=s.get_revision(db,row);snap=revision.snapshot
    state=g.review_state(db,row,revision)
    result={'id':row.id,'title':row.title,'number':row.number,'version':row.version,
            'technical_basis':snap.get('technical_basis'),'bom':[{k:v for k,v in line.items() if k in ('material_id','name','unit','quantity')} for line in snap['bom']],
            'note':snap['input']['note'],'quantity':snap['input']['quantity'],
            'state':state if g.financial_access(db,user) else {k:v for k,v in state.items() if k!='issues'}}
    if g.financial_access(db,user):
        result.update(result=snap['result'],overhead_plan=snap.get('overhead_plan'),facts=g.financial_facts(snap) if state['production_approved'] else [],documents=document_list(db,'estimate',id))
    result['history']=history_list(db,'estimate',id,g.financial_access(db,user))
    return result


@router.post('/estimates/{id}/{stage}')
def review_estimate(id:int,stage:Literal['production','accounting','director'],body:Review,db:Session=Depends(get_db),user=Depends(get_current_user)):
    g.require_person(db,user,stage);s.lock(db)
    row=target_row(db,'estimate',id,True)
    {'production':g.approve_production,'accounting':g.approve_accounting,'director':g.approve_director}[stage](db,user,row,body)
    return estimate(id,db,user)


class PlanSelection(Strict):
    version:int=Field(ge=1)
    plan_id:int=Field(gt=0)


@router.put('/estimates/{id}/overhead-plan')
def select_plan(id:int,body:PlanSelection,db:Session=Depends(get_db),user=Depends(get_current_user)):
    financial(db,user,True);s.lock(db);row=target_row(db,'estimate',id,True)
    if row.version!=body.version: s.fail('Версия расчёта изменилась',409)
    plan=target_row(db,'month',body.plan_id)
    if not g.plan_state(db,plan)['approved']: s.fail('Сначала бухгалтер и руководитель должны утвердить месячный план',409)
    payload=EstimateInput.model_validate({**s.get_revision(db,row).snapshot['input'],'request_id':str(uuid4()),
        'version':row.version,'overhead_plan_id':plan.id,'reason':'Привязка утверждённого месячного плана расходов'})
    from app.costing.router import save_estimate
    save_estimate(db,user,payload,row.id)
    return estimate(id,db,user)


@router.get('/month-plans')
def plans(db:Session=Depends(get_db),user=Depends(get_current_user)):
    financial(db,user)
    return [g.plan_state(db,row) for row in db.query(Plan).order_by(Plan.month.desc(),Plan.version.desc()).limit(120)]


@router.post('/month-plans',status_code=201)
def create_plan(body:OverheadInput,db:Session=Depends(get_db),user=Depends(get_current_user)):
    financial(db,user,True);s.lock(db)
    previous=db.query(Plan).filter_by(month=body.month).order_by(Plan.version.desc()).first()
    if (previous.version if previous else 0)!=body.previous_version: s.fail('У месяца уже есть другая версия плана',409)
    row=Plan(month=body.month,version=body.previous_version+1,actor_id=user.id,content=body.model_dump(mode='json',exclude={'month','previous_version'}))
    db.add(row);db.commit();return plan_detail(row.id,db,user)


@router.get('/month-plans/{id}')
def plan_detail(id:int,db:Session=Depends(get_db),user=Depends(get_current_user)):
    financial(db,user);row=target_row(db,'month',id)
    return {**g.plan_state(db,row),'facts':g.plan_facts(row),'documents':document_list(db,'month',id),'history':history_list(db,'month',id)}


@router.post('/month-plans/{id}/{stage}')
def approve_plan(id:int,stage:Literal['accounting','director'],body:Review,db:Session=Depends(get_db),user=Depends(get_current_user)):
    g.require_person(db,user,stage);s.lock(db);row=target_row(db,'month',id,True)
    if g.replay(db,user,'month',id,stage,body): return plan_detail(id,db,user)
    state=g.plan_state(db,row)
    if not state['current'] or row.version!=body.version: s.fail('Месячный план изменился',409)
    if stage=='accounting':
        evidence=g.document_proofs(db,'month',id,g.plan_facts(row),body.evidence)
        g.record(db,user,'month',id,stage,row.version,fingerprint(row.content),body,evidence=evidence)
    else:
        accounting=g.latest_signoff(db,'month',id,'accounting',fingerprint(row.content),row.version)
        if not accounting or not g.proofs_intact(db,accounting): s.fail('Месячные расходы и документы сначала проверяет бухгалтер',409)
        g.record(db,user,'month',id,stage,row.version,fingerprint(row.content),body,accounting_signoff_id=accounting.id)
    db.commit();return plan_detail(id,db,user)


@router.post('/documents/{target}/{id}',status_code=201)
async def upload_document(target:Literal['estimate','month'],id:int,file:UploadFile,db:Session=Depends(get_db),user=Depends(get_current_user)):
    financial(db,user,True);target_row(db,target,id)
    raw=await file.read(20*1024*1024+1)
    if not raw or len(raw)>20*1024*1024: s.fail('Загрузите PDF или скан до 20 МБ')
    try:
        if raw.startswith(b'%PDF-'):
            from pypdf import PdfReader
            pdf=PdfReader(BytesIO(raw))
            if pdf.is_encrypted or len(pdf.pages)==0: raise ValueError()
            content_type='application/pdf'
        else:
            from PIL import Image
            with Image.open(BytesIO(raw)) as image:
                if image.format not in ('PNG','JPEG','WEBP') or image.width*image.height>40_000_000: raise ValueError()
                content_type=Image.MIME[image.format];image.verify()
    except Exception: s.fail('Файл не распознан как доступный PDF или скан PNG/JPEG/WEBP')
    filename=(file.filename or 'Документ').replace('\\','/').split('/')[-1][:255]
    asset=save_bytes_file(db,filename,content_type,raw,FilePurpose.COSTING_EVIDENCE,user)
    row=Document(target=target,target_id=id,file_id=asset.id,actor_id=user.id,title=filename)
    db.add(row);db.commit();return {'id':row.id,'file_id':asset.id,'title':row.title,'sha256':asset.sha256,'size_bytes':asset.size_bytes}


@router.get('/documents/{id}/download')
def download_document(id:int,db:Session=Depends(get_db),user=Depends(get_current_user)):
    financial(db,user);doc=db.get(Document,id)
    if not doc: s.fail('Документ не найден',404)
    return verified_file_response(db.get(FileAsset,doc.file_id))
