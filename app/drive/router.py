from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import Field, field_validator
from sqlalchemy import or_
from sqlalchemy.orm import Session
from app.accounting.evidence_schemas import Strict
from app.collaboration.calendar_files import MAX_FILE, validate_file
from app.core.config import settings
from app.core.deps import get_current_user
from app.db.session import get_db
from app.users.models import User, UserRole
from app.warehouse.documents import advisory
from .models import Folder, DriveFile, Revision, Change

def private_response(response:Response):
    response.headers['Cache-Control']='private, no-store'
    response.headers['X-Content-Type-Options']='nosniff'


router = APIRouter(prefix='/api/drive', tags=['drive'], dependencies=[Depends(private_response)])
Scope = Literal['personal', 'company']


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def clean_name(value):
    value = value.strip()
    if not value or value in ('.', '..') or any(ord(c) < 32 or c in '/\\' for c in value):
        raise ValueError('Укажите имя без слешей и управляющих символов')
    return value


class Mutation(Strict):
    request_id: UUID
    version: int = Field(ge=1)


class FolderInput(Strict):
    request_id: UUID
    scope: Scope
    name: str = Field(min_length=1, max_length=200)
    _name = field_validator('name')(clean_name)


class Rename(Mutation):
    name: str = Field(min_length=1, max_length=255)
    _name = field_validator('name')(clean_name)


class Move(Mutation):
    folder_id: UUID | None = None


def visible(row, user):
    return row.scope == 'company' or row.owner_id == user.id


def editable(row, user):
    return row.owner_id == user.id or (row.scope == 'company' and user.role == UserRole.ADMIN)


def allowed_rows(model, user):
    return or_(model.scope == 'company', model.owner_id == user.id)


def folder(db, id, scope, user, *, lock=False):
    if id is None:
        return None
    query = db.query(Folder).filter_by(id=str(id))
    row = (query.with_for_update() if lock else query).first()
    if not row or not visible(row, user) or row.scope != scope:
        raise HTTPException(404, 'Папка не найдена или доступ закрыт')
    return row


def file_row(db, id, user, *, write=False):
    query = db.query(DriveFile).filter_by(id=str(id))
    row = (query.populate_existing().with_for_update() if write else query).first()
    if not row or not visible(row, user) or (row.deleted and not editable(row, user)):
        raise HTTPException(404, 'Файл не найден или доступ закрыт')
    if write and not editable(row, user):
        raise HTTPException(403, 'Изменять файл может автор или руководитель общего диска')
    return row


def person(db, id):
    user = db.get(User, id)
    return user.full_name if user else 'Сотрудник'


def revision_out(db, row):
    return {**{k:getattr(row, k) for k in ('id', 'number', 'filename', 'size', 'sha256', 'media_type')},
            'author':person(db, row.author_id), 'created_at':row.created_at.isoformat()}


def output(db, row, user):
    latest = db.query(Revision).filter_by(file_id=row.id).order_by(Revision.number.desc()).first()
    f = db.get(Folder, row.folder_id) if row.folder_id else None
    return {**{k:getattr(row, k) for k in ('id', 'name', 'scope', 'folder_id', 'version', 'deleted')},
            'owner':person(db, row.owner_id), 'can_edit':editable(row, user), 'folder_name':f.name if f else None,
            'updated_at':row.updated_at.isoformat(), 'revision':revision_out(db, latest)}


def retry(db, user, request_id, body):
    advisory(db, 'drive-request:'+str(user.id)+':'+str(request_id))
    old = db.query(Change).filter_by(actor_id=user.id, request_id=str(request_id)).first()
    if old and old.request_hash != digest(body):
        raise HTTPException(409, 'Этот запрос уже использован для другого изменения')
    return old


def record(db, user, request_id, body, action, *, row=None, target=None, data=None):
    query = db.query(Change)
    query = query.filter_by(file_id=row.id) if row else query.filter_by(folder_id=target.id)
    previous = query.order_by(Change.id.desc()).first()
    payload = {'action':action, 'actor_id':user.id, 'request_id':str(request_id), 'file_id':row.id if row else None,
               'folder_id':target.id if target else None, 'at':datetime.now(timezone.utc).isoformat(), 'details':data or {}}
    if row:payload['state']=file_state(row)
    previous_hash = previous.hash if previous else '0'*64
    db.add(Change(file_id=row.id if row else None, folder_id=target.id if target else None, actor_id=user.id,
                  request_id=str(request_id), request_hash=digest(body), action=action, data=payload,
                  previous_hash=previous_hash, hash=digest({'previous':previous_hash, 'data':payload})))


def file_state(row):
    return {k:getattr(row,k) for k in ('id','owner_id','folder_id','scope','name','version','deleted')}


@router.get('/folders')
def folders(scope:Scope, db:Session=Depends(get_db), user:User=Depends(get_current_user)):
    rows = db.query(Folder).filter(Folder.scope==scope, allowed_rows(Folder, user)).order_by(Folder.name, Folder.id).all()
    return [{'id':r.id, 'name':r.name, 'scope':r.scope, 'version':r.version, 'can_edit':editable(r,user)} for r in rows]


@router.post('/folders', status_code=201)
def create_folder(body:FolderInput, db:Session=Depends(get_db), user:User=Depends(get_current_user)):
    request = body.model_dump(mode='json')
    request['action'] = 'folder_create'
    old = retry(db, user, body.request_id, request)
    if old:
        row = folder(db, old.folder_id, body.scope, user)
    else:
        row = Folder(id=str(uuid4()), owner_id=user.id, scope=body.scope, name=body.name)
        db.add(row); db.flush()
        record(db,user,body.request_id,request,'folder_create',target=row,data={'name':row.name,'scope':row.scope})
        db.commit()
    return {'id':row.id, 'name':row.name, 'scope':row.scope, 'version':row.version, 'can_edit':editable(row,user)}


@router.get('/files')
def files(scope:Scope, folder_id:UUID|None=None, q:str=Query('',max_length=200), trash:bool=False,
          offset:int=Query(0,ge=0), limit:int=Query(50,ge=1,le=100), db:Session=Depends(get_db), user:User=Depends(get_current_user)):
    folder(db, folder_id, scope, user)
    query = db.query(DriveFile).filter(DriveFile.scope==scope, DriveFile.deleted==trash, allowed_rows(DriveFile,user))
    if trash and user.role != UserRole.ADMIN:
        query = query.filter(DriveFile.owner_id==user.id)
    if folder_id is not None or (not q.strip() and not trash):
        query = query.filter(DriveFile.folder_id==(str(folder_id) if folder_id else None))
    if q.strip():
        query = query.filter(DriveFile.name.icontains(q.strip(),autoescape=True))
    total=query.count()
    rows=query.order_by(DriveFile.updated_at.desc(),DriveFile.id).offset(offset).limit(limit).all()
    return {'items':[output(db,r,user) for r in rows], 'total':total, 'offset':offset, 'limit':limit}


def read_upload(file):
    try:
        name = clean_name(file.filename or '')
    except ValueError as e:
        raise HTTPException(422,str(e))
    if len(name)>255: raise HTTPException(422,'Имя файла слишком длинное')
    data=file.file.read(MAX_FILE+1)
    if not data or len(data)>MAX_FILE: raise HTTPException(413,'Файл должен быть размером от 1 байта до 50 МБ')
    media=validate_file(name,data)
    return name,data,media,hashlib.sha256(data).hexdigest()


def blob_path(id):
    return Path(settings.storage_dir)/'drive'/str(UUID(id))


@router.post('/files',status_code=201)
def upload(file:UploadFile=File(...),request_id:UUID=Form(...),scope:Scope=Form(...),folder_id:UUID|None=Form(None),
           file_id:UUID|None=Form(None),version:int|None=Form(None),db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    # Authorize the destination before reading potentially large content.
    if file_id:
        row=file_row(db,file_id,user)
        if row.scope!=scope: raise HTTPException(409,'Область доступа файла нельзя менять загрузкой')
    else: folder(db,folder_id,scope,user)
    name,data,media,sha=read_upload(file)
    request={'action':'upload','scope':scope,'folder_id':str(folder_id) if folder_id else None,
             'file_id':str(file_id) if file_id else None,'version':version,'name':name,'sha256':sha}
    old=retry(db,user,request_id,request)
    if old:return output(db,file_row(db,old.file_id,user),user)
    if file_id:
        row=file_row(db,file_id,user,write=True)
        if row.deleted or row.version!=version: raise HTTPException(409,'Файл изменён или находится в корзине. Обновите список.')
        previous=db.query(Revision).filter_by(file_id=row.id).order_by(Revision.number.desc()).first()
        if Path(name).suffix.lower()!=Path(previous.filename).suffix.lower():
            raise HTTPException(422,'Новая версия должна иметь тот же формат файла')
        row.version+=1
    else:
        folder(db,folder_id,scope,user,lock=True)
        row=DriveFile(id=str(uuid4()),owner_id=user.id,scope=scope,folder_id=str(folder_id) if folder_id else None,name=name,version=1,deleted=False)
        db.add(row);db.flush()
    row.updated_at=datetime.now(timezone.utc)
    revision=Revision(id=str(uuid4()),file_id=row.id,number=row.version,filename=name,size=len(data),sha256=sha,media_type=media,author_id=user.id)
    path=blob_path(revision.id);path.parent.mkdir(parents=True,exist_ok=True)
    try:
        with path.open('xb') as stream:stream.write(data)
        db.add(revision);db.flush()
        record(db,user,request_id,request,'upload' if not file_id else 'new_version',row=row,
               data={'name':row.name,'version':row.version,'revision_id':revision.id,'sha256':sha,'size':len(data),'scope':scope})
        db.commit()
    except Exception:
        db.rollback();path.unlink(missing_ok=True);raise
    return output(db,row,user)


def mutate(db,user,id,body,action):
    request={'action':action,'id':str(id),**body.model_dump(mode='json')}
    old=retry(db,user,body.request_id,request)
    row=file_row(db,id,user,write=True)
    if old:return row,True,request
    if row.version!=body.version:raise HTTPException(409,'Файл уже изменён другим пользователем. Обновите список.')
    if row.deleted and action!='restore':raise HTTPException(409,'Сначала восстановите файл из корзины')
    if not row.deleted and action=='restore':raise HTTPException(409,'Файл уже восстановлен')
    return row,False,request


def finish(db,user,row,body,request,action,before):
    row.version+=1;row.updated_at=datetime.now(timezone.utc)
    record(db,user,body.request_id,request,action,row=row,data={'before':before,'name':row.name,'folder_id':row.folder_id,'deleted':row.deleted,'version':row.version})
    db.commit()
    return output(db,row,user)


@router.patch('/files/{id}/name')
def rename(id:UUID,body:Rename,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    row,repeated,request=mutate(db,user,id,body,'rename')
    if repeated:return output(db,row,user)
    latest=db.query(Revision).filter_by(file_id=row.id).order_by(Revision.number.desc()).first()
    if Path(body.name).suffix.lower()!=Path(latest.filename).suffix.lower():raise HTTPException(422,'Сохраните расширение файла')
    before={'name':row.name};row.name=body.name
    return finish(db,user,row,body,request,'rename',before)


@router.patch('/files/{id}/folder')
def move(id:UUID,body:Move,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    row,repeated,request=mutate(db,user,id,body,'move')
    if repeated:return output(db,row,user)
    folder(db,body.folder_id,row.scope,user,lock=True)
    before={'folder_id':row.folder_id};row.folder_id=str(body.folder_id) if body.folder_id else None
    return finish(db,user,row,body,request,'move',before)


@router.post('/files/{id}/trash')
def trash_file(id:UUID,body:Mutation,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    row,repeated,request=mutate(db,user,id,body,'trash')
    if repeated:return output(db,row,user)
    row.deleted=True
    return finish(db,user,row,body,request,'trash',{'deleted':False})


@router.post('/files/{id}/restore')
def restore(id:UUID,body:Mutation,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    row,repeated,request=mutate(db,user,id,body,'restore')
    if repeated:return output(db,row,user)
    row.deleted=False
    return finish(db,user,row,body,request,'restore',{'deleted':True})


@router.get('/files/{id}/history')
def history(id:UUID,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    row=file_row(db,id,user)
    versions=db.query(Revision).filter_by(file_id=row.id).order_by(Revision.number.desc()).all()
    changes=db.query(Change).filter_by(file_id=row.id).order_by(Change.id).all()
    previous='0'*64;valid=bool(changes) and changes[0].action=='upload'
    for c in changes:
        valid=valid and c.previous_hash==previous and c.hash==digest({'previous':previous,'data':c.data})
        previous=c.hash
    valid=valid and changes[-1].data.get('state')==file_state(row)
    evidence={c.data['details'].get('revision_id'):c.data['details'] for c in changes if c.action in ('upload','new_version')}
    valid=valid and len(evidence)==len(versions) and all(evidence.get(r.id,{}).get('sha256')==r.sha256 and evidence.get(r.id,{}).get('size')==r.size for r in versions)
    return {'file':output(db,row,user),'versions':[revision_out(db,r) for r in versions], 'history_verified':valid,
            'changes':[{'id':c.id,'action':c.action,'actor':person(db,c.actor_id),'at':c.created_at.isoformat(),'details':c.data['details']} for c in reversed(changes)]}


@router.get('/files/{id}/download')
def download(id:UUID,revision_id:UUID|None=None,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    row=file_row(db,id,user)
    if row.deleted:raise HTTPException(409,'Сначала восстановите файл из корзины')
    query=db.query(Revision).filter_by(file_id=row.id)
    revision=query.filter_by(id=str(revision_id)).first() if revision_id else query.order_by(Revision.number.desc()).first()
    if not revision:raise HTTPException(404,'Версия не найдена')
    path=blob_path(revision.id)
    if not path.is_file() or path.stat().st_size!=revision.size or hashlib.sha256(path.read_bytes()).hexdigest()!=revision.sha256:
        raise HTTPException(409,'Файл недоступен или контрольная сумма не совпадает')
    return FileResponse(path,filename=revision.filename if revision_id else row.name,media_type='application/octet-stream',
                        headers={'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff','Content-Security-Policy':'sandbox'})
