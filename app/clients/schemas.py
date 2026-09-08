from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.clients.models import ClientStage, OrderType, PaymentPlan
from app.common.files import FileAssetOut


class ClientContact(BaseModel):
    """Один способ связи с клиентом: мессенджер/канал и адрес в нём."""

    messenger: str
    contact: str


class ClientCreate(BaseModel):
    full_name: str
    phone: str
    email: str
    contacts: list[ClientContact] = []
    max_chat_id: int | None = None


class ClientMaxChatUpdate(BaseModel):
    """Привязка переписки с клиентом к чату в мессенджере MAX. `null`
    отвязывает; `0` — «Избранное»."""

    max_chat_id: int | None = None


class ClientProjectUpdate(BaseModel):
    order_type: OrderType | None = None
    wishes_description: str | None = None
    estimated_price: float | None = None
    house_area: float | None = None
    layout_notes: str | None = None


class ClientDocumentsUpdate(BaseModel):
    final_price: float | None = None
    installation_address: str | None = None
    houses_count: int | None = None
    payment_plan: PaymentPlan | None = None
    advance_amount: float | None = None


class ClientPaymentUpdate(BaseModel):
    is_paid: bool


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
    max_chat_id: int | None

    order_type: OrderType | None
    wishes_description: str | None
    estimated_price: float | None
    house_area: float | None
    layout_notes: str | None
    project_locked_at: datetime | None

    houses_count: int
    final_price: float | None
    payment_plan: PaymentPlan
    advance_amount: float | None
    installation_address: str | None
    contract_file: FileAssetOut | None
    house_project_file: FileAssetOut | None
    documents_locked_at: datetime | None

    is_paid: bool | None
    payment_locked_at: datetime | None
    balance_paid: bool | None
    balance_paid_at: datetime | None

    notes: list[ClientNoteOut] = []
