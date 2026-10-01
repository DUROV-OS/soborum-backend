"""Бизнес-логика заявок «Пожелания/предложения» (0075)."""

from fastapi import UploadFile
from sqlalchemy.orm import Session

from app.common.files import FilePurpose, save_text_file, save_upload_file
from app.feedback.models import (
    FeedbackAttachment,
    FeedbackAttachmentKind,
    FeedbackRequest,
    FeedbackStatus,
)
from app.users.models import User, UserRole

MAX_SCREENSHOTS = 5
MAX_SCREENSHOT_BYTES = 10 * 1024 * 1024
MAX_LOG_CHARS = 200_000


def can_read(user: User, request: FeedbackRequest) -> bool:
    """Заявку видит её автор и любой администратор — больше никто."""
    return user.role == UserRole.ADMIN or request.author_id == user.id


def create_request(
    db: Session,
    author: User,
    *,
    text: str,
    section: str,
    screenshots: list[UploadFile],
    client_log: str | None,
) -> FeedbackRequest:
    """Создаёт заявку с вложениями. `ValueError` — на всё, что должно вернуться
    пользователю как 400 (не-изображение, слишком большой или лишний файл)."""
    if len(screenshots) > MAX_SCREENSHOTS:
        raise ValueError(f"Можно приложить не больше {MAX_SCREENSHOTS} скриншотов")

    request = FeedbackRequest(
        author_id=author.id,
        section=section.strip(),
        text=text.strip(),
        status=FeedbackStatus.NEW,
    )
    db.add(request)
    db.flush()

    for upload in screenshots:
        _validate_screenshot(upload)
        asset = save_upload_file(db, upload, FilePurpose.FEEDBACK_ATTACHMENT, author)
        db.add(
            FeedbackAttachment(
                feedback_id=request.id,
                file_asset_id=asset.id,
                kind=FeedbackAttachmentKind.SCREENSHOT,
            )
        )

    if client_log and client_log.strip():
        asset = save_text_file(
            db,
            f"logs-{author.id}-request-{request.id}.log",
            client_log[:MAX_LOG_CHARS],
            FilePurpose.FEEDBACK_ATTACHMENT,
            author,
        )
        db.add(
            FeedbackAttachment(
                feedback_id=request.id,
                file_asset_id=asset.id,
                kind=FeedbackAttachmentKind.LOG,
            )
        )

    db.commit()
    db.refresh(request)
    return request


def _validate_screenshot(upload: UploadFile) -> None:
    content_type = (upload.content_type or "").lower()
    if not content_type.startswith("image/"):
        raise ValueError(f"«{upload.filename}» — не изображение. К заявке прикладываются только скриншоты.")
    size = getattr(upload, "size", None)
    if size is not None and size > MAX_SCREENSHOT_BYTES:
        raise ValueError(f"«{upload.filename}» больше 10 МБ")


def list_requests(db: Session, user: User) -> list[FeedbackRequest]:
    """Администратору — все заявки, сотруднику — только свои. Новые сверху."""
    query = db.query(FeedbackRequest)
    if user.role != UserRole.ADMIN:
        query = query.filter(FeedbackRequest.author_id == user.id)
    return query.order_by(FeedbackRequest.id.desc()).all()


def set_status(db: Session, request: FeedbackRequest, status: FeedbackStatus) -> FeedbackRequest:
    request.status = status
    db.commit()
    db.refresh(request)
    return request
