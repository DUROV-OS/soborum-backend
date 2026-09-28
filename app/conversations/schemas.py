from datetime import datetime
from typing import Literal

from pydantic import BaseModel, field_validator

from app.conversations.models import ChannelKind, MessageDelivery, MessageDirection

ChannelState = Literal["NONE", "INVITED", "CONNECTED", "STOPPED"]


class ChannelStateOut(BaseModel):
    channel: ChannelKind
    # Есть ли на сервере токен бота этого мессенджера.
    configured: bool
    # NONE — приглашения ещё не было.
    status: ChannelState
    invite_link: str | None = None
    connected_name: str | None = None
    connected_at: datetime | None = None


class ConversationMessageOut(BaseModel):
    id: int
    channel: ChannelKind
    direction: MessageDirection
    text: str
    attachments: list
    author_id: int | None
    author_name: str | None
    delivery: MessageDelivery
    error: str | None
    sent_at: datetime | None
    created_at: datetime


class ConversationOut(BaseModel):
    channels: list[ChannelStateOut]
    messages: list[ConversationMessageOut]


class InviteCreate(BaseModel):
    channel: ChannelKind


class MessageCreate(BaseModel):
    # Основной канал — MAX (запрос заказчика): если у человека есть Макс,
    # переписка идёт туда.
    channel: ChannelKind = ChannelKind.MAX
    text: str

    @field_validator("text")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = (value or "").strip()
        if not value:
            raise ValueError("пустое сообщение")
        return value
