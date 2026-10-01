"""API заявок «Пожелания/предложения» (0075).

Подача — любому вошедшему сотруднику, разбор и смена статуса — только роли
`admin`. Файлы заявки отдаёт этот же раздел, а не общий `/api/files/{id}`:
право на них определяется авторством заявки, а не доступом к разделу."""

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.core.deps import get_current_user, require_admin
from app.db.session import get_db
from app.feedback.models import FeedbackAttachment, FeedbackRequest
from app.feedback.schemas import FeedbackRequestOut, FeedbackStatusUpdate
from app.feedback.service import can_read, create_request, list_requests, set_status
from app.users.models import User

app = FastAPI(
    title="Soborbum — Пожелания и предложения",
    description="Заявки сотрудников по работе системы: текст, раздел, скриншоты и лог сессии.",
    version="0.1.0",
)


@app.get("/requests", response_model=list[FeedbackRequestOut])
def list_feedback(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return [FeedbackRequestOut.from_model(r) for r in list_requests(db, user)]


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
    return FeedbackRequestOut.from_model(request)


@app.get("/requests/{request_id}", response_model=FeedbackRequestOut)
def get_feedback(request_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return FeedbackRequestOut.from_model(_readable_request(db, user, request_id))


@app.patch("/requests/{request_id}", response_model=FeedbackRequestOut)
def update_feedback_status(
    request_id: int,
    payload: FeedbackStatusUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    request = db.get(FeedbackRequest, request_id)
    if not request:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Заявка не найдена")
    return FeedbackRequestOut.from_model(set_status(db, request, payload.status))


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
