from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.feedback.models import FeedbackAttachmentKind, FeedbackStatus


class FeedbackAuthorOut(BaseModel):
    id: int
    full_name: str
    email: str


class FeedbackAttachmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    file_id: int
    filename: str
    content_type: str
    kind: FeedbackAttachmentKind


class FeedbackRequestOut(BaseModel):
    id: int
    section: str
    text: str
    status: FeedbackStatus
    created_at: datetime
    updated_at: datetime
    author: FeedbackAuthorOut
    attachments: list[FeedbackAttachmentOut]

    @staticmethod
    def from_model(request) -> "FeedbackRequestOut":
        return FeedbackRequestOut(
            id=request.id,
            section=request.section,
            text=request.text,
            status=request.status,
            created_at=request.created_at,
            updated_at=request.updated_at,
            author=FeedbackAuthorOut(
                id=request.author.id,
                full_name=request.author.full_name,
                email=request.author.email,
            ),
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
        )


class FeedbackStatusUpdate(BaseModel):
    status: FeedbackStatus
