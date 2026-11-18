from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Query, Response

from app.core.config import settings
from app.core.deps import get_current_user
from app.mail import service
from app.users.models import User, UserRole
from app.accounting.evidence_schemas import Strict

app = FastAPI(title="Durov OS — общая почта", dependencies=[Depends(get_current_user)])

def allowed(user):
    return user.role == UserRole.ADMIN or user.id in settings.shared_mail_allowed_user_ids

def require_mail(user: User = Depends(get_current_user)):
    if not allowed(user): raise HTTPException(403,'Нет доступа к общему ящику. Обратитесь к руководителю для назначения доступа.')
    return user


@app.middleware("http")
async def private_responses(request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.get("/status")
def status(user: User = Depends(get_current_user)):
    # Configured is not a successful connection check.
    return {"address": settings.shared_mail_address, "configured": service.configured(), "can_access": allowed(user), "mode": "shared"}

@app.get('/folders', dependencies=[Depends(require_mail)])
def folders():
    return service.list_folders()


@app.get("/messages", dependencies=[Depends(require_mail)])
def messages(before: int | None = Query(None, ge=1), limit: int = Query(30, ge=1, le=50), folder_id: str | None = Query(None,max_length=1400), q: str = Query('',max_length=200,pattern=r'^[^\x00-\x1f]*$')):
    return service.list_messages(before, limit, service.folder_name(folder_id), q)


@app.get("/messages/{uid}", dependencies=[Depends(require_mail)])
def message(uid: int, uidvalidity: str = Query(..., pattern=r"^[0-9]{1,20}$"), folder_id: str | None = Query(None,max_length=1400)):
    if uid < 1:
        raise HTTPException(422, "Некорректный идентификатор письма")
    return service.read_message(uid, uidvalidity, service.folder_name(folder_id))

class ReadFlags(Strict):
    read: bool

@app.patch('/messages/{uid}/flags', dependencies=[Depends(require_mail)])
def flags(uid: int, body: ReadFlags, uidvalidity: str = Query(...,pattern=r'^[0-9]{1,20}$'), folder_id: str | None = Query(None,max_length=1400)):
    if uid<1: raise HTTPException(422,'Некорректный идентификатор письма')
    return service.set_read(uid,uidvalidity,body.read,service.folder_name(folder_id))


@app.get("/messages/{uid}/attachments/{part_index}", dependencies=[Depends(require_mail)])
def attachment(uid: int, part_index: int, uidvalidity: str = Query(..., pattern=r"^[0-9]{1,20}$"), folder_id: str | None = Query(None,max_length=1400)):
    if uid < 1 or part_index < 0:
        raise HTTPException(422, "Некорректный идентификатор вложения")
    message = service.read_raw(uid, uidvalidity, service.folder_name(folder_id))
    for index, part in service.attachments(message):
        if index == part_index:
            name = part.get_filename() or "attachment"
            return Response(part.get_payload(decode=True) or b"", media_type="application/octet-stream",
                headers={"Content-Disposition": "attachment; filename=attachment; filename*=UTF-8''" + quote(name, safe="")})
    raise HTTPException(404, "Вложение не найдено")
