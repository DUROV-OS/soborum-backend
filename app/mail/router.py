from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Query, Response

from app.core.config import settings
from app.core.deps import get_current_user
from app.mail import service

app = FastAPI(title="Durov OS — общая почта", dependencies=[Depends(get_current_user)])


@app.middleware("http")
async def private_responses(request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.get("/status")
def status():
    # Configured is not a successful connection check.
    return {"address": settings.shared_mail_address, "configured": service.configured(), "mode": "read_only"}


@app.get("/messages")
def messages(before: int | None = Query(None, ge=1), limit: int = Query(30, ge=1, le=50)):
    return service.list_messages(before, limit)


@app.get("/messages/{uid}")
def message(uid: int, uidvalidity: str = Query(..., pattern=r"^[0-9]{1,20}$")):
    if uid < 1:
        raise HTTPException(422, "Некорректный идентификатор письма")
    return service.read_message(uid, uidvalidity)


@app.get("/messages/{uid}/attachments/{part_index}")
def attachment(uid: int, part_index: int, uidvalidity: str = Query(..., pattern=r"^[0-9]{1,20}$")):
    if uid < 1 or part_index < 0:
        raise HTTPException(422, "Некорректный идентификатор вложения")
    message = service.read_raw(uid, uidvalidity)
    for index, part in service.attachments(message):
        if index == part_index:
            name = part.get_filename() or "attachment"
            return Response(part.get_payload(decode=True) or b"", media_type="application/octet-stream",
                headers={"Content-Disposition": "attachment; filename=attachment; filename*=UTF-8''" + quote(name, safe="")})
    raise HTTPException(404, "Вложение не найдено")
