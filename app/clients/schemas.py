from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.clients.models import ClientChatState, ClientStage, OrderType, PaymentPlan
from app.common.files import FileAssetOut
from app.tasks.models import TaskReportKind, TaskStatus
from app.house_models.schemas import HouseModelBriefOut


class ClientContact(BaseModel):
    """Один способ связи с клиентом: мессенджер/канал и адрес в нём."""

    messenger: str
    contact: str


class ClientSourceUpdate(BaseModel):
    """Источник клиента: пришёл сам или его привело агентство-партнёр (0079-c).

    `agency_name` обязательно при `via_agency = True`; при `via_agency = False`
    поля агентства чистятся, чтобы у клиента, помеченного прямым, не осталось
    названия от прошлой правки."""

    via_agency: bool = False
    agency_name: str | None = None
    agency_contact: str | None = None


class ClientCreate(ClientSourceUpdate):
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


class ClientTaskCreate(BaseModel):
    """Задача менеджера по клиенту (0079-d): «связаться», «выслать каталог»,
    «уточнить по ипотеке». Срок обязателен — задача без срока теряется.
    `blocking` по умолчанию True: пока такая задача открыта, клиента нельзя
    перевести на следующую стадию."""

    title: str
    description: str | None = None
    deadline: datetime
    assignee_ids: list[int] = []
    blocking: bool = True


class ClientTaskDeadlineUpdate(BaseModel):
    """Перенос срока. Причина обязательна: именно по ней руководство потом
    видит, почему клиент стоит."""

    deadline: datetime
    reason: str


class ClientTaskClose(BaseModel):
    """Закрытие задачи с описанием решения и, по желанию, сразу вытекающей
    задачей — типичный ход работы с клиентом: «дозвонился, просит каталог» →
    новая задача «выслать каталог»."""

    resolution: str
    next_task: ClientTaskCreate | None = None


class ClientTaskReportOut(BaseModel):
    """Строка журнала задачи: решение по задаче или перенос срока."""

    id: int
    kind: TaskReportKind
    comment: str
    author_id: int
    created_at: datetime


class ClientTaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    description: str | None
    deadline: datetime | None
    status: TaskStatus
    blocking: bool
    # Стадия клиента на момент постановки задачи — по ней видно, на каком
    # шаге пути клиент застрял.
    stage: ClientStage | None
    assignee_ids: list[int] = []
    reports: list[ClientTaskReportOut] = []

    @model_validator(mode="before")
    @classmethod
    def _from_orm_task(cls, value):
        """`blocking`, `stage` и `assignee_ids` живут не колонками задачи, а в
        её `link_meta` и связях — собираем их, когда на вход пришла сама
        задача, а не готовый словарь."""
        if isinstance(value, dict) or not hasattr(value, "link_meta"):
            return value
        return ClientTaskOut.from_task(value).model_dump()

    @staticmethod
    def from_task(task) -> "ClientTaskOut":
        meta = task.link_meta or {}
        raw_stage = meta.get("stage")
        return ClientTaskOut(
            id=task.id,
            title=task.title,
            description=task.description,
            deadline=task.deadline,
            status=task.status,
            blocking=bool(meta.get("blocking", True)),
            stage=ClientStage(raw_stage) if raw_stage in {s.value for s in ClientStage} else None,
            assignee_ids=[u.id for u in task.assignees],
            reports=[
                ClientTaskReportOut(
                    id=r.id,
                    kind=r.kind,
                    comment=r.comment,
                    author_id=r.author_id,
                    created_at=r.created_at,
                )
                for r in task.reports
            ],
        )


class ClientOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: int
    cycle_id: int
    stage: ClientStage
    created_at: datetime

    full_name: str
    phone: str
    email: str
    contacts: list[ClientContact] = []
    chat_links: list[ClientChatLinkOut] = []

    via_agency: bool
    agency_name: str | None
    agency_contact: str | None

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
    # Задачи менеджера по клиенту (0079-d): и открытые, и закрытые, свежие
    # сверху. Ближайшую открытую доска выбирает сама — отдельная сводка ради
    # этого не нужна, задач у клиента единицы.
    tasks: list[ClientTaskOut] = Field(default=[], validation_alias="followup_tasks")
