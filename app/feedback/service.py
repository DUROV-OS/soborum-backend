"""Бизнес-логика заявок «Пожелания/предложения» (0075)."""

from fastapi import UploadFile
from sqlalchemy.orm import Session

from app.common.files import FilePurpose, save_text_file, save_upload_file
from app.common.module_access import Module
from app.feedback.models import (
    FeedbackAttachment,
    FeedbackAttachmentKind,
    FeedbackEvent,
    FeedbackEventKind,
    FeedbackRequest,
    FeedbackStatus,
)
from app.notifications.models import NotificationKind
from app.notifications.service import notify
from app.users.models import User, UserRole

MAX_SCREENSHOTS = 5
MAX_SCREENSHOT_BYTES = 10 * 1024 * 1024
MAX_LOG_CHARS = 200_000
MAX_EVENT_CHARS = 5000


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


def list_requests(db: Session, user: User, *, mine: bool = False) -> list[FeedbackRequest]:
    """Администратору — все заявки, сотруднику — только свои. Новые сверху.
    `mine` — только свои для любой роли («Мои заявки», 0090)."""
    query = db.query(FeedbackRequest)
    if mine or user.role != UserRole.ADMIN:
        query = query.filter(FeedbackRequest.author_id == user.id)
    return query.order_by(FeedbackRequest.id.desc()).all()


_FEEDBACK_STATUS_LABELS: dict[FeedbackStatus, str] = {
    FeedbackStatus.NEW: "новая",
    FeedbackStatus.IN_PROGRESS: "в работе",
    FeedbackStatus.DONE: "выполнена",
    FeedbackStatus.REJECTED: "отклонена",
}


def set_status(db: Session, request: FeedbackRequest, status: FeedbackStatus, actor: User) -> FeedbackRequest:
    """Меняет статус и пишет смену в ленту заявки (0090). Повторная установка
    того же статуса в ленте не отражается."""
    if request.status != status:
        db.add(
            FeedbackEvent(
                feedback_id=request.id,
                author_id=actor.id,
                kind=FeedbackEventKind.STATUS,
                old_status=request.status,
                new_status=status,
            )
        )
        request.status = status
        # Уведомление автору заявки (0080-c). У раздела «Пожелания» нет
        # своего Module в матрице доступа (см. докстрока модели — подача
        # доступна всем, разбор только админам, грант тут нечего выдавать) —
        # для мьюта используем Module.TASKS как наиболее близкий по смыслу
        # (заявка — это тикет для разбора, а не раздел с данными); отдельный
        # Module под один тип уведомления заводить не стали.
        if request.author_id != actor.id:
            notify(
                db,
                request.author_id,
                kind=NotificationKind.FEEDBACK_UPDATE,
                module=Module.TASKS,
                title=f"Статус вашей заявки изменён на «{_FEEDBACK_STATUS_LABELS.get(status, status.value)}»",
                object_type="feedback_request",
                object_id=request.id,
            )
    db.commit()
    db.refresh(request)
    return request


def can_add_event(user: User, request: FeedbackRequest, kind: FeedbackEventKind) -> bool:
    """Комментарий — администратор или автор заявки; «изменение в системе» —
    только администратор: что поменяли в системе, решает не автор."""
    if user.role == UserRole.ADMIN:
        return True
    return kind == FeedbackEventKind.COMMENT and request.author_id == user.id


def add_event(
    db: Session, request: FeedbackRequest, actor: User, *, kind: FeedbackEventKind, text: str
) -> FeedbackRequest:
    """Комментарий (администратора или автора) или «изменение в системе» в
    ленту заявки. Права проверяет вызывающий (`can_add_event`). `ValueError` —
    на пустой или слишком длинный текст и на попытку записать смену статуса
    вручную."""
    if kind not in (FeedbackEventKind.COMMENT, FeedbackEventKind.CHANGE):
        raise ValueError("В ленту можно добавить только комментарий или изменение в системе")
    text = text.strip()
    if not text:
        raise ValueError("Текст записи обязателен")
    if len(text) > MAX_EVENT_CHARS:
        raise ValueError(f"Текст записи длиннее {MAX_EVENT_CHARS} символов")
    db.add(FeedbackEvent(feedback_id=request.id, author_id=actor.id, kind=kind, text=text))
    db.commit()
    db.refresh(request)
    return request


def unseen_updates(request: FeedbackRequest, user: User) -> int:
    """Сколько записей ленты автор ещё не видел. Для не-автора — 0: счётчик
    существует только в «Моих заявках». Свои же записи (админ комментирует
    собственную заявку) непрочитанными не считаются."""
    if request.author_id != user.id:
        return 0
    seen = request.author_seen_event_id or 0
    return sum(1 for e in request.events if e.id > seen and e.author_id != user.id)


def mark_seen(db: Session, request: FeedbackRequest) -> FeedbackRequest:
    """Автор открыл заявку — всё в ленте на этот момент прочитано."""
    if request.events:
        request.author_seen_event_id = request.events[-1].id
        db.commit()
        db.refresh(request)
    return request
