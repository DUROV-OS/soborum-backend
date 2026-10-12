from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.common.module_access import Module
from app.notifications.models import NotificationKind


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: NotificationKind
    title: str
    body: str | None
    object_type: str | None
    object_id: int | None
    created_at: datetime
    read_at: datetime | None


class UnreadCountOut(BaseModel):
    unread_count: int


class NotificationMuteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    module: Module
    object_id: int | None
    created_at: datetime


class NotificationMuteUpdate(BaseModel):
    """Тело `PUT /mutes`: включить или снять один мьют — раздел целиком
    (`object_id` не задан) или конкретный объект в разделе."""

    module: Module
    object_id: int | None = None
    muted: bool
