from datetime import datetime

from pydantic import BaseModel, ConfigDict


class BotInfoOut(BaseModel):
    configured: bool
    username: str | None = None
    watched_chat_id: str | None = None


class DeepLinkOut(BaseModel):
    url: str
    token: str
    expires_at: datetime
    bot_username: str | None = None


class TelegramLinkOut(BaseModel):
    linked: bool
    tg_user_id: str | None = None
    tg_username: str | None = None
    alias: str | None = None
    user_id: int | None = None
    linked_at: datetime | None = None


class LinkUpsertIn(BaseModel):
    tg_user_id: str
    user_id: int | None = None
    tg_username: str | None = None
    alias: str | None = None


class TelegramMessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    message_id: int
    sent_at: datetime
    sender_name: str | None = None
    tg_username: str | None = None
    kind: str
    text: str | None = None
    file_asset_id: int | None = None
    ingested_into_kb_at: datetime | None = None


class JobRunOut(BaseModel):
    name: str
    status: str
    detail: str = ""
