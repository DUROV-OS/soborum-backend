"""API заявок «Пожелания/предложения» (0075).

Подача — любому вошедшему сотруднику, разбор и смена статуса — только роли
`admin`. Файлы заявки отдаёт этот же раздел, а не общий `/api/files/{id}`:
право на них определяется авторством заявки, а не доступом к разделу."""

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.core.deps import get_current_user, require_admin
from app.db.session import get_db
from app.feedback.models import FeedbackAttachment, FeedbackRequest
from app.feedback.schemas import FeedbackEventCreate, FeedbackRequestOut, FeedbackStatusUpdate
from app.feedback.service import (
    add_event,
    can_add_event,
    can_read,
    create_request,
    list_requests,
    mark_seen,
    set_status,
    unseen_updates,
)
from app.users.models import User

app = FastAPI(
    title="Soborbum — Пожелания и предложения",
    description="Заявки сотрудников по работе системы: текст, раздел, скриншоты и лог сессии.",
    version="0.1.0",
)


def _out(request: FeedbackRequest, user: User) -> FeedbackRequestOut:
    return FeedbackRequestOut.from_model(request, unseen_updates(request, user))


@app.get("/requests", response_model=list[FeedbackRequestOut])
def list_feedback(
    mine: bool = Query(default=False, description="Только свои заявки — для «Моих заявок», в т.ч. у админа"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return [_out(r, user) for r in list_requests(db, user, mine=mine)]


@app.post("/requests", response_model=FeedbackRequestOut, status_code=status.HTTP_201_CREATED)
def create_feedback(
    text: str = Form(..., min_length=1),
    section: str = Form(..., min_length=1),
    screenshots: list[UploadFile] = File(default=[]),
    client_log: str | None = Form(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if not text.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Текст заявки обязателен"
        )
    try:
        request = create_request(
            db,
            user,
            text=text,
            section=section,
            screenshots=[f for f in screenshots if f and f.filename],
            client_log=client_log,
        )
    except ValueError as error:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    return _out(request, user)


@app.get("/requests/{request_id}", response_model=FeedbackRequestOut)
def get_feedback(request_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _out(_readable_request(db, user, request_id), user)


@app.patch("/requests/{request_id}", response_model=FeedbackRequestOut)
def update_feedback_status(
    request_id: int,
    payload: FeedbackStatusUpdate,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    request = db.get(FeedbackRequest, request_id)
    if not request:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Заявка не найдена")
    return _out(set_status(db, request, payload.status, admin), admin)


@app.post(
    "/requests/{request_id}/events",
    response_model=FeedbackRequestOut,
    status_code=status.HTTP_201_CREATED,
)
def add_feedback_event(
    request_id: int,
    payload: FeedbackEventCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Запись в ленту заявки (0090): комментарий — администратор или автор
    заявки, «изменение в системе» — только администратор."""
    request = _readable_request(db, user, request_id)
    if not can_add_event(user, request, payload.kind):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Изменение в системе отмечает только администратор",
        )
    try:
        request = add_event(db, request, user, kind=payload.kind, text=payload.text)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error
    return _out(request, user)


@app.post("/requests/{request_id}/seen", response_model=FeedbackRequestOut)
def mark_feedback_seen(request_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Автор открыл заявку в «Моих заявках» — обновления прочитаны (0090).
    Администратор, открывая чужую заявку, счётчик автора не сбрасывает."""
    request = _readable_request(db, user, request_id)
    if request.author_id != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Отметить прочитанной может только автор")
    return _out(mark_seen(db, request), user)


@app.get("/requests/{request_id}/files/{file_id}")
def download_feedback_file(
    request_id: int,
    file_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    request = _readable_request(db, user, request_id)
    attachment = (
        db.query(FeedbackAttachment)
        .filter(
            FeedbackAttachment.feedback_id == request.id,
            FeedbackAttachment.file_asset_id == file_id,
        )
        .first()
    )
    if not attachment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Файл заявки не найден")
    asset = attachment.file_asset
    return FileResponse(asset.path_on_disk, media_type=asset.content_type, filename=asset.filename)


def _readable_request(db: Session, user: User, request_id: int) -> FeedbackRequest:
    request = db.get(FeedbackRequest, request_id)
    if not request:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Заявка не найдена")
    if not can_read(user, request):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Нет доступа к этой заявке")
    return request
