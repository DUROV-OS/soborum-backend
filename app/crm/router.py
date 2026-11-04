from fastapi import FastAPI, Depends, Query
from fastapi.responses import FileResponse
from fastapi import HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field, ConfigDict
from app.core.deps import require_view, require_edit, require_full
from app.common.module_access import Module
from app.db.session import get_db
from app.users.models import User
from app.crm import service
from app.crm import communications
from app.max import service as max_service
from app.crm.models import CRMEntity
from app.crm.schemas import Entry, Patch, Note
from app.moysklad.models import SourceRecord
from app.moysklad.service import attachment_path

app = FastAPI(title="Продажи — CRM Соборума")
view = require_view(Module.CLIENTS)
edit = require_edit(Module.CLIENTS)
full = require_full(Module.CLIENTS)


class ChatLinkInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    chat_id: int
    label: str = Field(min_length=1, max_length=200)


class ChatMessageInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    text: str = Field(min_length=1, max_length=4000)


@app.get('/entries/{id}/chats')
def card_chats(id: int, db: Session = Depends(get_db), user: User = Depends(view)):
    return communications.links(db, id)


@app.post('/entries/{id}/chats')
def attach_chat(id: int, body: ChatLinkInput, db: Session = Depends(get_db), user: User = Depends(edit)):
    # Resolve the chosen chat before saving; no message is sent by attachment.
    communications.card(db, id, editing=True)
    max_service.get_chat(body.chat_id, limit=1)
    return communications.attach(db, id, body.chat_id, body.label, user)


@app.delete('/entries/{id}/chats/{link_id}')
def detach_chat(id: int, link_id: int, db: Session = Depends(get_db), user: User = Depends(edit)):
    return communications.detach(db, id, link_id, user)


@app.get('/entries/{id}/chats/{chat_id}/messages')
def card_messages(id: int, chat_id: int, db: Session = Depends(get_db), user: User = Depends(view)):
    communications.require_chat(db, id, chat_id)
    return max_service.get_chat(chat_id, limit=80)


@app.post('/entries/{id}/chats/{chat_id}/messages', status_code=201)
def send_card_message(id: int, chat_id: int, body: ChatMessageInput, db: Session = Depends(get_db), user: User = Depends(edit)):
    communications.require_chat(db, id, chat_id, editing=True)
    if not body.text.strip():
        raise HTTPException(422, 'Введите сообщение')
    return max_service.send_message(chat_id, body.text.strip())


@app.get('/attention')
def attention(
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    reason: str | None = Query(None,pattern='^(inquiry|overdue|no_next_step|no_deadline|no_owner|idle)$'),
    db: Session = Depends(get_db),
    user: User = Depends(view),
):
    from app.crm.guard import attention

    return attention(db, offset=offset, limit=limit, reason=reason)


@app.get('/settings')
def settings(db: Session = Depends(get_db), user: User = Depends(view)):
    from app.crm.guard import settings

    return settings(db)


class GuardSettings(BaseModel):
    model_config=ConfigDict(extra='forbid')
    response_hours:int=Field(ge=1,le=168)
    idle_days:int=Field(ge=1,le=90)


@app.put('/settings')
def update_settings(body: GuardSettings, db: Session = Depends(get_db), user: User = Depends(full)):
    from app.crm.models import CRMSettings

    row = db.get(CRMSettings, 1)
    if row is None:
        row = CRMSettings(id=1)
        db.add(row)
    row.response_hours = body.response_hours
    row.idle_days = body.idle_days
    db.commit()
    return body.model_dump()


@app.get("/files")
def files(
    query: str = Query("", max_length=300),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(view),
):
    return service.files(db, query, offset=offset, limit=limit)


@app.get("/files/{id}/download")
def download(id: int, db: Session = Depends(get_db), user: User = Depends(view)):
    row = db.get(SourceRecord, id)
    if not row or row.entity_type not in ("amocrm/files", "amocrm/file_versions"):
        raise HTTPException(404, "Файл не найден")
    path, item = attachment_path(row, 0)
    return FileResponse(
        path,
        filename=item["filename"],
        media_type=item.get("content_type", "application/octet-stream"),
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


@app.get("/catalog")
def catalog(db: Session = Depends(get_db), user: User = Depends(view)):
    rows = (
        db.query(CRMEntity)
        .filter(CRMEntity.kind.in_(["pipeline", "stage"]), CRMEntity.archived.is_(False))
        .all()
    )
    return {
        "pipelines": [service.out(r) for r in rows if r.kind == "pipeline"],
        "stages": [service.out(r) for r in rows if r.kind == "stage"],
        "users": [
            {"id": u.id, "name": u.full_name}
            for u in db.query(User).filter_by(is_active=True)
            if u.has_access(Module.CLIENTS)
        ],
        "counts": {
            kind: service.list_entries(db, kind, limit=1)["total"]
            for kind in ["deal", "contact", "company", "task", "inquiry"]
        },
    }


@app.get("/entries")
def entries(
    kind: str = "deal",
    query: str = Query("", max_length=300),
    archive: str = Query("active", pattern="^(active|archived|all)$"),
    pipeline_id: int | None = None,
    stage_id: int | None = None,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(view),
):
    return service.list_entries(db, kind, query, archive, pipeline_id, stage_id, limit, offset)


@app.get("/entries/{id}")
def entry(id: int, db: Session = Depends(get_db), user: User = Depends(view)):
    return service.out(service.get(db, id))


@app.post("/entries/{kind}", status_code=201)
def create(kind: str, entry: Entry, db: Session = Depends(get_db), user: User = Depends(edit)):
    if kind in ("stage", "pipeline"):
        full(user)
    return service.create(db, kind, entry, user)


@app.patch("/entries/{id}")
def patch(id: int, entry: Patch, db: Session = Depends(get_db), user: User = Depends(edit)):
    row = service.get(db, id)
    if row.kind in ("stage", "pipeline") or entry.archived is not None:
        full(user)
    return service.patch(db, id, entry, user)


@app.get("/entries/{id}/history")
def history(
    id: int,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(view),
):
    return service.history(db, service.get(db, id), limit, offset)


@app.get("/entries/{id}/files")
def entry_files(
    id: int, offset: int = Query(0, ge=0), db: Session = Depends(get_db), user: User = Depends(view)
):
    return service.files(db, entity=service.get(db, id), offset=offset)


@app.post("/entries/{id}/notes", status_code=201)
def note(id: int, entry: Note, db: Session = Depends(get_db), user: User = Depends(edit)):
    return service.add_note(db, service.get(db, id), entry.text, user)


@app.post("/entries/{id}/accept")
def accept(id: int, entry: Entry, db: Session = Depends(get_db), user: User = Depends(edit)):
    return service.accept_inquiry(db, service.get(db, id), entry, user)
