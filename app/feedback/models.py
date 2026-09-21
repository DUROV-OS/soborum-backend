"""Заявки «Пожелания/предложения» (0075): сотрудник описывает проблему или
идею, прикладывает скриншоты и лог своей сессии, администратор разбирает.

Раздел намеренно без своего `Module`: подача доступна любому вошедшему, а
разбор — только роли `admin`, отдельный грант тут нечего выдавать."""

import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.common.files import FileAsset
from app.db.base import Base


class FeedbackStatus(str, enum.Enum):
    NEW = "new"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    REJECTED = "rejected"


class FeedbackAttachmentKind(str, enum.Enum):
    SCREENSHOT = "screenshot"
    LOG = "log"


class FeedbackRequest(Base):
    __tablename__ = "feedback_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    # Слаг раздела фронта (`SectionId`), а не `Module`: заявка может касаться
    # экрана без своего бэкового раздела («Пульс», «Работа», «Агенты»).
    section: Mapped[str] = mapped_column(String(64), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[FeedbackStatus] = mapped_column(
        Enum(FeedbackStatus, name="feedback_status"), nullable=False, default=FeedbackStatus.NEW
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    author: Mapped["User"] = relationship()  # noqa: F821
    attachments: Mapped[list["FeedbackAttachment"]] = relationship(
        back_populates="request", cascade="all, delete-orphan", order_by="FeedbackAttachment.id"
    )


class FeedbackAttachment(Base):
    __tablename__ = "feedback_attachments"

    id: Mapped[int] = mapped_column(primary_key=True)
    feedback_id: Mapped[int] = mapped_column(
        ForeignKey("feedback_requests.id", ondelete="CASCADE"), nullable=False, index=True
    )
    file_asset_id: Mapped[int] = mapped_column(ForeignKey("file_assets.id"), nullable=False)
    kind: Mapped[FeedbackAttachmentKind] = mapped_column(
        Enum(FeedbackAttachmentKind, name="feedback_attachment_kind"), nullable=False
    )

    request: Mapped["FeedbackRequest"] = relationship(back_populates="attachments")
    file_asset: Mapped[FileAsset] = relationship()
