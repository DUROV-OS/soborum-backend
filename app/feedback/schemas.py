from datetime import datetime

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.feedback.models import FeedbackAttachmentKind, FeedbackEventKind, FeedbackStatus


class FeedbackAuthorOut(BaseModel):
    id: int
    full_name: str
    email: str

    @staticmethod
    def from_user(user) -> "FeedbackAuthorOut":
        return FeedbackAuthorOut(id=user.id, full_name=user.full_name, email=user.email)


class FeedbackAttachmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    file_id: int
    filename: str
    content_type: str
    kind: FeedbackAttachmentKind


class FeedbackEventOut(BaseModel):
    id: int
    kind: FeedbackEventKind
    text: str | None
    old_status: FeedbackStatus | None
    new_status: FeedbackStatus | None
    author: FeedbackAuthorOut
    created_at: datetime


class FeedbackRequestOut(BaseModel):
    id: int
    section: str
    text: str
    status: FeedbackStatus
    created_at: datetime
    updated_at: datetime
    author: FeedbackAuthorOut
    attachments: list[FeedbackAttachmentOut]
    # Лента разбора заявки по возрастанию времени и число записей, которых
    # автор ещё не видел (для не-автора всегда 0) — 0090.
    events: list[FeedbackEventOut]
    unseen_updates: int = 0

    @staticmethod
    def from_model(request, unseen_updates: int = 0) -> "FeedbackRequestOut":
        return FeedbackRequestOut(
            id=request.id,
            section=request.section,
            text=request.text,
            status=request.status,
            created_at=request.created_at,
            updated_at=request.updated_at,
            author=FeedbackAuthorOut.from_user(request.author),
            attachments=[
                FeedbackAttachmentOut(
                    id=a.id,
                    file_id=a.file_asset_id,
                    filename=a.file_asset.filename,
                    content_type=a.file_asset.content_type,
                    kind=a.kind,
                )
                for a in request.attachments
            ],
            events=[
                FeedbackEventOut(
                    id=e.id,
                    kind=e.kind,
                    text=e.text,
                    old_status=e.old_status,
                    new_status=e.new_status,
                    author=FeedbackAuthorOut.from_user(e.author),
                    created_at=e.created_at,
                )
                for e in request.events
            ],
            unseen_updates=unseen_updates,
        )


class FeedbackStatusUpdate(BaseModel):
    status: FeedbackStatus


class FeedbackEventCreate(BaseModel):
    """Запись в ленту: комментарий (админ или автор) или изменение в системе
    (только админ). `status` вручную не создаётся — его
    пишет смена статуса."""

    kind: Literal[FeedbackEventKind.COMMENT, FeedbackEventKind.CHANGE]
    text: str = Field(min_length=1)
