"""Internal collaboration. No external messaging; all reads enforce project membership."""
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4
import hashlib
import json
import re

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.common.module_access import AccessLevel, Module
from app.core.config import settings
from app.core.deps import get_current_user
from app.db.session import get_db
from app.users.models import User, UserRole
from app.tasks import service as tasks
from app.tasks.models import Task, TaskStatus
from app.tasks.schemas import TaskOut, TaskScope
from .models import Project, Member, ProjectMessage as Message, ProjectFile, ProjectTask, ProjectEvent

app = FastAPI(title='Durov OS — Проекты')
MAX_FILE = 10 * 1024 * 1024


def fail(code, detail):
    raise HTTPException(code, detail)


def fingerprint(body):
    data = body.model_dump(mode='json')
    data.pop('request_id', None)
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def person(db, id):
    user = db.get(User, id)
    return {'id': id, 'name': user.full_name if user else 'Сотрудник', 'active': bool(user and user.is_active)}


def editable(user):
    return user.role == UserRole.ADMIN or any(user.access_level(module) >= AccessLevel.EDIT for module in (Module.TASKS, Module.CLIENTS, Module.PRODUCTION))


def project(db, id, user, *, write=False, manage=False):
    # Serialize mutations with membership changes, including uploads and retries.
    query = db.query(Project).filter_by(id=id)
    row = query.with_for_update().first() if write else query.first()
    if not row or (user.role != UserRole.ADMIN and not db.get(Member, (id, user.id))):
        fail(404, 'Проект не найден или доступ закрыт')
    if manage and user.role != UserRole.ADMIN and row.owner_id != user.id:
        fail(403, 'Изменять проект и участников может ответственный или руководитель')
    if write and not manage and row.status != 'active':
        fail(409, 'Проект закрыт. Ответственный может вернуть его в работу.')
    return row


def audit(db, row, actor, kind, details):
    db.add(ProjectEvent(project_id=row.id, actor_id=actor.id, kind=kind, details=details))


def users(db, ids):
    values = set(ids)
    if len(values) > 100:
        fail(422, 'В проекте может быть до 100 участников')
    found = db.query(User).filter(User.id.in_(values), User.is_active.is_(True)).all()
    if len(found) != len(values):
        fail(422, 'Один из сотрудников не найден или отключён')
    return found


class ProjectInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default='', max_length=10000)
    owner_id: int = Field(gt=0)
    member_ids: list[int] = Field(default_factory=list, max_length=100)
    client_id: int | None = Field(default=None, gt=0)
    deal_id: int | None = Field(default=None, gt=0)
    production_id: int | None = Field(default=None, gt=0)
    deadline: date | None = None

    @field_validator('name')
    @classmethod
    def name_present(cls, value):
        if not value.strip():
            raise ValueError('Укажите название проекта')
        return value.strip()


class CreateProject(ProjectInput):
    request_id: UUID


class UpdateProject(ProjectInput):
    version: int = Field(gt=0)
    status: Literal['active', 'completed', 'archived']


def validate_links(db, body, user, previous=None):
    from app.clients.models import Client
    from app.production.models import Production
    from app.crm.models import CRMEntity
    for key, model, module in [('client_id', Client, Module.CLIENTS), ('production_id', Production, Module.PRODUCTION), ('deal_id', CRMEntity, Module.CLIENTS)]:
        value = getattr(body, key)
        if previous and value == getattr(previous, key):
            continue
        if (value is not None or previous and getattr(previous, key) is not None) and not user.has_access(module):
            fail(403, 'Нет доступа к связываемому разделу')
        if value is not None:
            linked = db.get(model, value)
            if not linked or (key == 'deal_id' and (linked.kind != 'deal' or linked.archived)):
                fail(422, 'Связанная запись не найдена или недоступна')
    if body.production_id and body.client_id:
        production = db.get(Production, body.production_id)
        if not production.cycle.client or production.cycle.client.id != body.client_id:
            fail(422, 'Производство относится к другому клиенту')


def output(db, row, actor, detail=False):
    member = db.get(Member, (row.id, actor.id))
    unread = db.query(func.count(Message.id)).filter(Message.project_id == row.id, Message.id > member.last_read_id, Message.author_id != actor.id).scalar() if member else 0
    result = {key: getattr(row, key) for key in ['id', 'name', 'description', 'status', 'deadline', 'version', 'created_at', 'owner_id']}
    result.update(owner=person(db, row.owner_id), unread=unread, can_manage=actor.role == UserRole.ADMIN or actor.id == row.owner_id,
                  can_write=row.status == 'active', can_create_task=row.status == 'active' and actor.access_level(Module.TASKS) >= AccessLevel.EDIT)
    result['links'] = []
    # Links do not confer permissions or include customer/private cost fields.
    for key, section, path, label in [('client_id', Module.CLIENTS, '/clients/', 'Клиент'), ('production_id', Module.PRODUCTION, '/production/', 'Производство'), ('deal_id', Module.CLIENTS, '/crm?tab=deal&entry=', 'Сделка')]:
        value = getattr(row, key)
        if value and actor.has_access(section):
            result['links'].append({'kind': key, 'id': value, 'label': f'{label} №{value}', 'path': path + str(value)})
    if detail:
        result['members'] = [person(db, item.user_id) for item in db.query(Member).filter_by(project_id=row.id).order_by(Member.user_id)]
        result['files'] = [file_out(db, item) for item in db.query(ProjectFile).filter_by(project_id=row.id).order_by(ProjectFile.id.desc()).limit(200)]
        result['events'] = [{'id': item.id, 'kind': item.kind, 'actor': person(db, item.actor_id), 'created_at': item.created_at, 'details': item.details} for item in db.query(ProjectEvent).filter_by(project_id=row.id).order_by(ProjectEvent.id.desc()).limit(50)]
        result['task_count'] = db.query(func.count(ProjectTask.id)).filter_by(project_id=row.id).scalar()
    return result


@app.get('/options')
def options(query: str = Query('', max_length=100), db: Session = Depends(get_db), actor: User = Depends(get_current_user)):
    from app.clients.models import Client
    from app.production.models import Production
    from app.crm.models import CRMEntity
    employees = db.query(User).filter(User.is_active.is_(True)).order_by(User.full_name).limit(500).all()
    result = {'people': [dict(person(db, u.id), can_task=u.access_level(Module.TASKS) >= AccessLevel.EDIT) for u in employees], 'clients': [], 'productions': [], 'deals': [], 'can_create': editable(actor), 'can_link_clients': actor.has_access(Module.CLIENTS), 'can_link_production': actor.has_access(Module.PRODUCTION)}
    if actor.has_access(Module.CLIENTS):
        result['clients'] = [{'id': row.id, 'name': row.full_name} for row in db.query(Client).filter(Client.full_name.contains(query, autoescape=True)).order_by(Client.id.desc()).limit(50)]
        result['deals'] = [{'id': row.id, 'name': row.name} for row in db.query(CRMEntity).filter(CRMEntity.kind == 'deal', CRMEntity.archived.is_(False), CRMEntity.name.contains(query, autoescape=True)).order_by(CRMEntity.id.desc()).limit(50)]
    if actor.has_access(Module.PRODUCTION):
        result['productions'] = [{'id': row.id, 'name': row.name} for row in db.query(Production).filter(Production.name.contains(query, autoescape=True)).order_by(Production.id.desc()).limit(50)]
    return result


@app.get('/')
def list_projects(query: str = Query('', max_length=100), status: Literal['active', 'completed', 'archived', 'all'] = 'active', offset: int = Query(0, ge=0), db: Session = Depends(get_db), actor: User = Depends(get_current_user)):
    rows = db.query(Project)
    if actor.role != UserRole.ADMIN:
        rows = rows.join(Member, Member.project_id == Project.id).filter(Member.user_id == actor.id)
    if status != 'all':
        rows = rows.filter(Project.status == status)
    rows = rows.filter(Project.name.contains(query, autoescape=True))
    return {'total': rows.count(), 'items': [output(db, row, actor) for row in rows.order_by(Project.id.desc()).offset(offset).limit(50)], 'can_create': editable(actor)}


@app.post('/')
def create(body: CreateProject, db: Session = Depends(get_db), actor: User = Depends(get_current_user)):
    if not editable(actor):
        fail(403, 'Для создания проекта нужны права редактирования задач, клиентов или производства')
    existing = db.query(Project).filter_by(created_by_id=actor.id, request_id=str(body.request_id)).first()
    if existing:
        project(db, existing.id, actor)
        if existing.request_hash != fingerprint(body):
            fail(409, 'Этот запрос уже использован для другого проекта')
        return output(db, existing, actor, True)
    users(db, [body.owner_id, actor.id, *body.member_ids]); validate_links(db, body, actor)
    row = Project(**body.model_dump(exclude={'member_ids', 'request_id'}), created_by_id=actor.id, request_id=str(body.request_id), request_hash=fingerprint(body))
    db.add(row)
    try:
        db.flush()
        for id in set([actor.id, body.owner_id, *body.member_ids]):
            db.add(Member(project_id=row.id, user_id=id))
        audit(db, row, actor, 'created', {'name': row.name, 'members': sorted(set([actor.id, body.owner_id, *body.member_ids]))})
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.query(Project).filter_by(created_by_id=actor.id, request_id=str(body.request_id)).first()
        if not existing or existing.request_hash != fingerprint(body):
            fail(409, 'Конфликт создания проекта. Обновите список.')
        project(db, existing.id, actor)
        row = existing
    return output(db, row, actor, True)


@app.get('/{id}')
def detail(id: int, db: Session = Depends(get_db), actor: User = Depends(get_current_user)):
    return output(db, project(db, id, actor), actor, True)


@app.patch('/{id}')
def update(id: int, body: UpdateProject, db: Session = Depends(get_db), actor: User = Depends(get_current_user)):
    row = project(db, id, actor, write=True, manage=True)
    if row.version != body.version:
        fail(409, 'Проект уже изменён. Обновите карточку перед сохранением.')
    for key in ('client_id', 'deal_id', 'production_id'):
        if key not in body.model_fields_set:
            setattr(body, key, getattr(row, key))
    members = set([body.owner_id, *body.member_ids]); users(db, members); validate_links(db, body, actor, row)
    if body.status == 'completed' and db.query(ProjectTask).join(Task, Task.id == ProjectTask.task_id).filter(ProjectTask.project_id == id, Task.status != TaskStatus.DONE).first():
        fail(409, 'Сначала завершите связанные задачи')
    previous = {'name': row.name, 'status': row.status, 'owner_id': row.owner_id, 'members': sorted(item.user_id for item in db.query(Member).filter_by(project_id=id))}
    for key, value in body.model_dump(exclude={'member_ids', 'version'}).items():
        setattr(row, key, value)
    row.version += 1
    for item in db.query(Member).filter_by(project_id=id).all():
        if item.user_id not in members:
            db.delete(item)
    for uid in members:
        if not db.get(Member, (id, uid)):
            db.add(Member(project_id=id, user_id=uid))
    audit(db, row, actor, 'updated', {'before': previous, 'after': {'name': row.name, 'status': row.status, 'owner_id': row.owner_id, 'members': sorted(members)}})
    db.commit()
    # A former owner may have explicitly transferred the project and left it.
    if actor.role != UserRole.ADMIN and actor.id not in members:
        return {'removed': True}
    return output(db, row, actor, True)


class SendMessage(BaseModel):
    body: str = Field(default='', max_length=10000)
    reply_to_id: int | None = Field(default=None, gt=0)
    file_ids: list[int] = Field(default_factory=list, max_length=10)
    mention_ids: list[int] = Field(default_factory=list, max_length=100)
    request_id: UUID


def file_out(db, row):
    return {'id': row.id, 'filename': row.filename, 'size': row.size, 'sha256': row.sha256, 'uploaded_by': person(db, row.uploaded_by_id), 'created_at': row.created_at}


def message_out(db, row):
    files = db.query(ProjectFile).filter(ProjectFile.id.in_(row.file_ids), ProjectFile.project_id == row.project_id).all() if row.file_ids else []
    return {'id': row.id, 'body': row.body, 'author': person(db, row.author_id), 'created_at': row.created_at, 'reply_to_id': row.reply_to_id, 'mentions': [person(db, id) for id in row.mention_ids], 'files': [file_out(db, file) for file in files]}


@app.get('/{id}/messages')
def messages(id: int, query: str = Query('', max_length=200), before: int = Query(0, ge=0), after: int = Query(0, ge=0), db: Session = Depends(get_db), actor: User = Depends(get_current_user)):
    project(db, id, actor)
    rows = db.query(Message).filter_by(project_id=id)
    if query:
        rows = rows.filter(Message.body.contains(query, autoescape=True))
    if before:
        rows = rows.filter(Message.id < before)
    if after:
        rows = rows.filter(Message.id > after)
    found = rows.order_by(Message.id.asc() if after else Message.id.desc()).limit(101).all()
    more = len(found) > 100; found = found[:100]
    if not after:
        found.reverse()
    return {'items': [message_out(db, row) for row in found], 'has_more': more}


@app.post('/{id}/messages')
def send(id: int, body: SendMessage, db: Session = Depends(get_db), actor: User = Depends(get_current_user)):
    row = project(db, id, actor, write=True)
    old = db.query(Message).filter_by(project_id=id, author_id=actor.id, request_id=str(body.request_id)).first()
    if old:
        if old.request_hash != fingerprint(body):
            fail(409, 'Ключ отправки уже использован для другого сообщения')
        return message_out(db, old)
    if not body.body.strip() and not body.file_ids:
        fail(422, 'Введите сообщение или приложите файл')
    if body.reply_to_id and not db.query(Message).filter_by(id=body.reply_to_id, project_id=id).first():
        fail(422, 'Исходное сообщение не относится к проекту')
    if set(body.mention_ids) - {m.user_id for m in db.query(Member).filter_by(project_id=id)}:
        fail(422, 'Упомянуть можно только участника проекта')
    if len(set(body.file_ids)) != db.query(ProjectFile).filter(ProjectFile.project_id == id, ProjectFile.id.in_(body.file_ids)).count():
        fail(422, 'Файл не относится к проекту')
    item = Message(project_id=id, author_id=actor.id, body=body.body.strip(), reply_to_id=body.reply_to_id, file_ids=sorted(set(body.file_ids)), mention_ids=sorted(set(body.mention_ids)), request_id=str(body.request_id), request_hash=fingerprint(body))
    db.add(item); db.flush(); audit(db, row, actor, 'message', {'message_id': item.id}); db.commit()
    return message_out(db, item)


class ReadMessages(BaseModel):
    message_id: int = Field(gt=0)


@app.post('/{id}/read')
def read(id: int, body: ReadMessages, db: Session = Depends(get_db), actor: User = Depends(get_current_user)):
    project(db, id, actor)
    if not db.query(Message).filter_by(id=body.message_id, project_id=id).first():
        fail(422, 'Сообщение не относится к проекту')
    member = db.query(Member).filter_by(project_id=id, user_id=actor.id).with_for_update().first()
    if member:
        member.last_read_id = max(member.last_read_id, body.message_id); db.commit()
    return {'ok': True}


@app.post('/{id}/files')
def upload(id: int, file: UploadFile = File(...), request_id: UUID = Form(...), db: Session = Depends(get_db), actor: User = Depends(get_current_user)):
    row = project(db, id, actor, write=True)
    filename = re.split(r'[/\\]', file.filename or 'document')[-1][:255]
    extension = Path(filename).suffix.lower()
    if extension not in {'.pdf', '.png', '.jpg', '.jpeg', '.docx', '.xlsx', '.txt', '.csv'}:
        fail(422, 'Поддерживаются PDF, PNG, JPEG, DOCX, XLSX, TXT и CSV')
    data = file.file.read(MAX_FILE + 1)
    if not data or len(data) > MAX_FILE:
        fail(413, 'Размер файла должен быть от 1 байта до 10 МБ')
    magic = {'.pdf': b'%PDF-', '.png': b'\x89PNG\r\n\x1a\n', '.jpg': b'\xff\xd8\xff', '.jpeg': b'\xff\xd8\xff', '.docx': b'PK\x03\x04', '.xlsx': b'PK\x03\x04'}
    if extension in magic and not data.startswith(magic[extension]):
        fail(422, 'Содержимое файла не соответствует расширению')
    if extension in {'.txt', '.csv'}:
        try:
            data.decode('utf-8-sig')
        except UnicodeDecodeError:
            fail(422, 'Текстовый файл должен быть в UTF-8')
    digest = hashlib.sha256(data).hexdigest()
    old = db.query(ProjectFile).filter_by(project_id=id, uploaded_by_id=actor.id, request_id=str(request_id)).first()
    if old:
        if old.sha256 != digest or old.filename != filename:
            fail(409, 'Ключ загрузки уже использован для другого файла')
        return file_out(db, old)
    directory = Path(settings.storage_dir) / 'collaboration'; directory.mkdir(parents=True, exist_ok=True)
    destination = directory / str(uuid4())
    try:
        with destination.open('xb') as stream:
            stream.write(data)
        item = ProjectFile(project_id=id, uploaded_by_id=actor.id, filename=filename, path=str(destination), size=len(data), sha256=digest, request_id=str(request_id))
        db.add(item); db.flush(); audit(db, row, actor, 'file', {'file_id': item.id, 'filename': filename, 'sha256': digest}); db.commit()
    except Exception:
        db.rollback(); destination.unlink(missing_ok=True); raise
    return file_out(db, item)


@app.get('/{id}/files/{file_id}')
def download(id: int, file_id: int, db: Session = Depends(get_db), actor: User = Depends(get_current_user)):
    project(db, id, actor)
    item = db.query(ProjectFile).filter_by(id=file_id, project_id=id).first()
    if not item:
        fail(404, 'Файл не найден')
    path = Path(item.path)
    if not path.is_file() or path.stat().st_size != item.size or hashlib.sha256(path.read_bytes()).hexdigest() != item.sha256:
        fail(409, 'Файл недоступен или контрольная сумма не совпадает')
    return FileResponse(path, filename=item.filename, media_type='application/octet-stream', headers={'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff'})


class ProjectTaskInput(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    assignee_id: int = Field(gt=0)
    reviewer_id: int = Field(gt=0)
    deadline: datetime | None = None
    source_message_id: int | None = Field(default=None, gt=0)
    request_id: UUID


@app.post('/{id}/tasks')
def create_task(id: int, body: ProjectTaskInput, db: Session = Depends(get_db), actor: User = Depends(get_current_user)):
    row = project(db, id, actor, write=True)
    if actor.access_level(Module.TASKS) < AccessLevel.EDIT:
        fail(403, 'Нет права создавать задачи')
    old = db.query(ProjectTask).filter_by(project_id=id, created_by_id=actor.id, request_id=str(body.request_id)).first()
    if old:
        if old.request_hash != fingerprint(body):
            fail(409, 'Ключ создания уже использован')
        return {'task_id': old.task_id}
    if not body.title.strip() or body.assignee_id == body.reviewer_id:
        fail(422, 'Нужны название задачи и проверяющий, отличный от исполнителя')
    members = {m.user_id for m in db.query(Member).filter_by(project_id=id)}
    if not {body.assignee_id, body.reviewer_id}.issubset(members):
        fail(422, 'Исполнитель и проверяющий должны быть участниками проекта')
    selected = users(db, [body.assignee_id, body.reviewer_id])
    if any(user.access_level(Module.TASKS) < AccessLevel.EDIT for user in selected):
        fail(422, 'Исполнителю и проверяющему нужны права редактирования задач')
    if body.source_message_id and not db.query(Message).filter_by(id=body.source_message_id, project_id=id).first():
        fail(422, 'Исходное сообщение не относится к проекту')
    # The project discussion remains private. Native task title is explicitly
    # entered by the author; no message body, files or project name are copied.
    task = tasks.create_task(db, title=body.title.strip(), deadline=body.deadline, assignee_ids=[body.assignee_id], reviewer_ids=[body.reviewer_id], responsible_id=body.reviewer_id)
    db.add(ProjectTask(project_id=id, task_id=task.id, source_message_id=body.source_message_id, created_by_id=actor.id, request_id=str(body.request_id), request_hash=fingerprint(body)))
    audit(db, row, actor, 'task', {'task_id': task.id, 'source_message_id': body.source_message_id}); db.commit()
    return {'task_id': task.id}


@app.get('/{id}/tasks')
def project_tasks(id: int, db: Session = Depends(get_db), actor: User = Depends(get_current_user)):
    project(db, id, actor)
    if not actor.has_access(Module.TASKS):
        return []
    linked = db.query(ProjectTask).filter_by(project_id=id).all()
    found = db.query(Task).filter(Task.id.in_([row.task_id for row in linked])).all()
    visible = tasks.apply_scope(found, actor, TaskScope.ALL if actor.has_access(Module.TASKS_ALL) else TaskScope.MINE)
    sources = {row.task_id: row.source_message_id for row in linked}
    return [{'task': TaskOut.from_model(task), 'source_message_id': sources[task.id]} for task in visible]
