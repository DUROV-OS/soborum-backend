from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.clients.models import ClientChatState, ClientStage, OrderType, PaymentPlan
from app.common.files import FileAssetOut
from app.house_models.schemas import HouseModelBriefOut


class ClientContact(BaseModel):
    """Один способ связи с клиентом: мессенджер/канал и адрес в нём."""

    messenger: str
    contact: str


class ClientCreate(BaseModel):
    full_name: str
    phone: str
    email: str
    contacts: list[ClientContact] = []


class ClientChatLinkCreate(BaseModel):
    """Новая привязка клиента к чату MAX (0053). `max_chat_id` уникален
    глобально — 409, если чат уже занят другим клиентом (см.
    client_service.create_chat_link). `0` — «Избранное»."""

    max_chat_id: int
    label: str


class ClientChatLinkUpdate(BaseModel):
    label: str | None = None
    state: ClientChatState | None = None


class ClientChatLinkOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    client_id: int
    max_chat_id: int
    label: str
    state: ClientChatState | None
    created_at: datetime


class ClientDocumentsUpdate(BaseModel):
    order_type: OrderType | None = None
    house_model_key: str | None = None
    final_price: float | None = None
    installation_address: str | None = None
    payment_plan: PaymentPlan | None = None
    advance_amount: float | None = None


class ClientHousesCountUpdate(BaseModel):
    houses_count: int


class ClientPaymentUpdate(BaseModel):
    is_paid: bool


class ClientPaymentEditUnlockUpdate(BaseModel):
    unlocked: bool


class ClientBalancePaymentUpdate(BaseModel):
    balance_paid: bool


class ClientNoteCreate(BaseModel):
    text: str


class ClientNoteUpdate(BaseModel):
    text: str


class ClientNoteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    client_id: int
    author_id: int
    text: str
    created_at: datetime


class ClientOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    cycle_id: int
    stage: ClientStage
    created_at: datetime

    full_name: str
    phone: str
    email: str
    contacts: list[ClientContact] = []
    chat_links: list[ClientChatLinkOut] = []

    order_type: OrderType | None
    house_model_key: str | None
    house_model: HouseModelBriefOut | None

    houses_count: int
    final_price: float | None
    payment_plan: PaymentPlan
    advance_amount: float | None
    installation_address: str | None
    contract_file: FileAssetOut | None
    contract_appendix_file: FileAssetOut | None
    house_project_file: FileAssetOut | None
    ar_file: FileAssetOut | None
    kr_file: FileAssetOut | None
    documents_locked_at: datetime | None

    is_paid: bool | None
    payment_locked_at: datetime | None
    payment_edit_unlocked: bool
    balance_paid: bool | None
    balance_paid_at: datetime | None

    notes: list[ClientNoteOut] = []
