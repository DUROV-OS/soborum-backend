"""Shared file storage: any section that needs to attach a document or media
file (contracts, house projects, task images, marketing material) stores it
here as a FileAsset and references it by id. Kept out of any single business
section since it's used by several of them."""

import enum
import os
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import DateTime, Enum, ForeignKey, String, func
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.core.config import settings
from app.core.deps import get_current_user
from app.common.module_access import Module
from app.db.base import Base
from app.db.session import get_db


class FilePurpose(str, enum.Enum):
    CONTRACT = "contract"
    HOUSE_PROJECT = "house_project"
    TASK_IMAGE = "task_image"
    MARKETING_RAW = "marketing_raw"
    MARKETING_FINAL = "marketing_final"
    AI_CHAT_ATTACHMENT = "ai_chat_attachment"
    TELEGRAM_INGEST = "telegram_ingest"


PURPOSE_MODULE = {
    FilePurpose.CONTRACT: Module.CLIENTS,
    FilePurpose.HOUSE_PROJECT: Module.CLIENTS,
    FilePurpose.TASK_IMAGE: Module.TASKS,
    FilePurpose.MARKETING_RAW: Module.MARKETING,
    FilePurpose.MARKETING_FINAL: Module.MARKETING,
    FilePurpose.AI_CHAT_ATTACHMENT: Module.AI,
    # Media pulled out of the watched Telegram group by the daily ingest.
    FilePurpose.TELEGRAM_INGEST: Module.AI,
}


class FileAsset(Base):
    __tablename__ = "file_assets"

    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(127), nullable=False)
    path_on_disk: Mapped[str] = mapped_column(String(500), nullable=False)
    purpose: Mapped[FilePurpose] = mapped_column(Enum(FilePurpose, name="file_purpose"), nullable=False)
    uploaded_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    uploaded_by: Mapped["User"] = relationship()  # noqa: F821


class FileAssetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    filename: str
    content_type: str
    purpose: FilePurpose
    uploaded_by_id: int
    created_at: datetime


def save_text_file(db: Session, filename: str, content: str, purpose: FilePurpose, user) -> FileAsset:
    """Persist AI-generated (or otherwise in-memory) text content as a FileAsset,
    for cases with no incoming UploadFile - see save_upload_file for that case."""
    os.makedirs(settings.storage_dir, exist_ok=True)
    disk_name = f"{uuid.uuid4().hex}_{filename}"
    path_on_disk = os.path.join(settings.storage_dir, disk_name)
    with open(path_on_disk, "w", encoding="utf-8") as f:
        f.write(content)

    asset = FileAsset(
        filename=filename,
        content_type="text/plain; charset=utf-8",
        path_on_disk=path_on_disk,
        purpose=purpose,
        uploaded_by_id=user.id,
    )
    db.add(asset)
    db.flush()
    return asset


def save_bytes_file(
    db: Session, filename: str, data: bytes, content_type: str, purpose: FilePurpose, user
) -> FileAsset:
    """Persist already-in-memory binary content (e.g. a file downloaded from
    the Telegram Bot API) as a FileAsset. Binary sibling of save_text_file."""
    os.makedirs(settings.storage_dir, exist_ok=True)
    disk_name = f"{uuid.uuid4().hex}_{filename}"
    path_on_disk = os.path.join(settings.storage_dir, disk_name)
    with open(path_on_disk, "wb") as f:
        f.write(data)

    asset = FileAsset(
        filename=filename,
        content_type=content_type or "application/octet-stream",
        path_on_disk=path_on_disk,
        purpose=purpose,
        uploaded_by_id=user.id,
    )
    db.add(asset)
    db.flush()
    return asset


def save_upload_file(db: Session, upload_file: UploadFile, purpose: FilePurpose, user) -> FileAsset:
    os.makedirs(settings.storage_dir, exist_ok=True)
    disk_name = f"{uuid.uuid4().hex}_{upload_file.filename}"
    path_on_disk = os.path.join(settings.storage_dir, disk_name)
    with open(path_on_disk, "wb") as f:
        f.write(upload_file.file.read())

    asset = FileAsset(
        filename=upload_file.filename or disk_name,
        content_type=upload_file.content_type or "application/octet-stream",
        path_on_disk=path_on_disk,
        purpose=purpose,
        uploaded_by_id=user.id,
    )
    db.add(asset)
    db.flush()
    return asset


router = APIRouter(tags=["files"])


@router.get("/files/{file_id}")
def download_file(file_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    asset = db.get(FileAsset, file_id)
    if not asset:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Файл не найден")
    required_module = PURPOSE_MODULE.get(asset.purpose)
    if required_module and not user.has_access(required_module):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Нет доступа к этому файлу")
    return FileResponse(asset.path_on_disk, media_type=asset.content_type, filename=asset.filename)
