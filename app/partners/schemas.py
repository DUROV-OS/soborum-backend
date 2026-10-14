from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator

from app.clients.models import ClientStage
from app.partners.models import PartnerCategory


class PartnerContact(BaseModel):
    """Один способ связи: мессенджер/канал и адрес в нём (как ClientContact)."""

    messenger: str
    contact: str


def _required_text(value: str) -> str:
    value = (value or "").strip()
    if not value:
        raise ValueError("поле обязательно")
    return value


def _optional_text(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


class PartnerCreate(BaseModel):
    category: PartnerCategory
    name: str
    city: str
    organization: str | None = None
    phone: str | None = None
    email: str | None = None
    contacts: list[PartnerContact] = []
    comment: str | None = None

    _required = field_validator("name", "city")(_required_text)
    _optional = field_validator("organization", "phone", "email", "comment")(_optional_text)


class PartnerUpdate(BaseModel):
    """Частичное изменение: переданы только те поля, что меняются. Данные
    партнёра, в отличие от базовых данных клиента, не замораживаются."""

    category: PartnerCategory | None = None
    name: str | None = None
    city: str | None = None
    organization: str | None = None
    phone: str | None = None
    email: str | None = None
    contacts: list[PartnerContact] | None = None
    comment: str | None = None

    @field_validator("name", "city")
    @classmethod
    def _required_if_given(cls, value: str | None) -> str | None:
        # None = «не менять»; пустая строка — попытка стереть обязательное поле.
        return None if value is None else _required_text(value)

    _optional = field_validator("organization", "phone", "email", "comment")(_optional_text)


class PartnerNoteCreate(BaseModel):
    text: str

    _required = field_validator("text")(_required_text)


class PartnerNoteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    partner_id: int
    author_id: int
    text: str
    created_at: datetime


class PartnerChatLinkCreate(BaseModel):
    """Новая привязка партнёра к чату MAX (0105). `max_chat_id` уникален
    глобально среди привязок партнёров и не должен совпадать с уже занятым
    клиентом чатом — см. `partner_service.create_chat_link`. `0` — «Избранное»."""

    max_chat_id: int
    label: str


class PartnerChatLinkOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    partner_id: int
    max_chat_id: int
    label: str
    created_at: datetime


class PartnerBriefOut(BaseModel):
    """Партнёр в чужой выдаче — например, «кто рекомендовал» у клиента (0083-c)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    category: PartnerCategory
    city: str
    organization: str | None


class ReferredClientOut(BaseModel):
    """Клиент, которого привёл партнёр (0083-c) — для блока в карточке партнёра."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    full_name: str
    stage: ClientStage
    created_at: datetime


class PartnerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    category: PartnerCategory
    name: str
    city: str
    organization: str | None
    phone: str | None
    email: str | None
    contacts: list[PartnerContact]
    comment: str | None
    created_by_id: int | None
    created_at: datetime
    updated_at: datetime | None
    notes: list[PartnerNoteOut] = []
    chat_links: list[PartnerChatLinkOut] = []
