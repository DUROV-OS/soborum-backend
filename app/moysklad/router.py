from fastapi import Depends, FastAPI, Query
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from app.core.deps import get_current_user
from app.db.session import get_db
from app.users.models import User
from app.moysklad import service

app = FastAPI(title="МойСклад — перенесённые данные")
from app.moysklad.recipes import router as recipes_router

app.include_router(recipes_router)


@app.get("/catalog")
def catalog(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return service.catalog(db, user)


@app.get("/records")
def records(
    query: str = Query("", max_length=300),
    entity_type: str | None = None,
    archive: str = Query("active", pattern="^(active|archived|all)$"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return service.search(db, user, query, entity_type, archive, limit, offset)


@app.get("/records/{record_id}")
def record(record_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    r = service.get_record(db, user, record_id)
    return {**service.brief(r), "payload": r.payload}


@app.get("/records/{record_id}/files/{index}")
def file(record_id: int, index: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    r = service.get_record(db, user, record_id)
    path, item = service.attachment_path(r, index)
    return FileResponse(
        path,
        filename=item["filename"],
        media_type=item.get("content_type", "application/octet-stream"),
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )
