from fastapi import FastAPI, Depends, Query
from fastapi.responses import FileResponse
from fastapi import HTTPException
from sqlalchemy.orm import Session
from app.core.deps import require_view, require_edit, require_full
from app.common.module_access import Module
from app.db.session import get_db
from app.users.models import User
from app.crm import service
from app.crm.models import CRMEntity
from app.crm.schemas import Entry, Patch, Note
from app.moysklad.models import SourceRecord
from app.moysklad.service import attachment_path

app = FastAPI(title="Продажи — CRM Соборума")
view = require_view(Module.CLIENTS)
edit = require_edit(Module.CLIENTS)
full = require_full(Module.CLIENTS)


@app.get('/attention')
def attention(
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(view),
):
    from app.crm.guard import attention

    return attention(db, offset=offset, limit=limit)


@app.get('/settings')
def settings(db: Session = Depends(get_db), user: User = Depends(view)):
    from app.crm.guard import settings

    return settings(db)


from pydantic import BaseModel,Field,ConfigDict
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
