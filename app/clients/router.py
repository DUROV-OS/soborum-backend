import logging
from urllib.parse import parse_qsl

from fastapi import Depends, FastAPI, HTTPException, Request, UploadFile, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.clients import intake_auth
from app.clients import reconcile as client_reconcile
from app.clients import service as client_service
from app.clients.models import Client, ClientNote, ClientStage
from app.clients.schemas import (
    ClientBalanceDueDateUpdate,
    ClientBasicUpdate,
    ClientBalancePaymentUpdate,
    ClientChatLinkCreate,
    ClientChatLinkOut,
    ClientChatLinkUpdate,
    ClientContractVerify,
    ClientManagerOut,
    ClientCreate,
    LeadIntakeOut,
    LeadIntakePayload,
    ClientDocumentsUpdate,
    ClientHousesCountUpdate,
    ClientManagerUpdate,
    ClientNoteCreate,
    ClientNoteOut,
    ClientNoteUpdate,
    ClientOut,
    ClientPaymentEditUnlockUpdate,
    ClientPaymentUpdate,
    ClientSourceUpdate,
    ClientTaskClose,
    ClientTaskCreate,
    ClientTaskDeadlineUpdate,
    ClientTaskOut,
)
from app.common.files import FilePurpose, save_upload_file
from app.common.module_access import Module
from app.core.deps import require_admin, require_edit, require_full, require_view
from app.db.session import get_db
from app.users.models import User

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Soborbum — Клиенты",
    description="Клиенты от лида до постоплаты: базовые, проектные, "
    "документные данные, формат расчёта, оплата, привязка к чату MAX и заметки.",
    version="0.6.0",
)

# Пилот 4-уровневого доступа (0052-a): просмотр — все GET; редактирование —
# создание/изменение через POST/PATCH; полный доступ — удаление клиента/заметки
# и служебный reconcile (раньше — require_admin, см. журнал задачи).
require_clients_view = require_view(Module.CLIENTS)
require_clients_edit = require_edit(Module.CLIENTS)
require_clients_full = require_full(Module.CLIENTS)


@app.get("/", response_model=list[ClientOut])
def list_clients(
    db: Session = Depends(get_db),
    _: User = Depends(require_clients_view),
    stage: ClientStage | None = None,
    search: str | None = None,
):
    """`search` — поиск по фамилии/имени и номеру телефона (0079-f), по всем
    стадиям сразу."""
    query = db.query(Client)
    if stage is not None:
        query = query.filter(Client.stage == stage)
    return client_service.search_clients(query.order_by(Client.id.desc()).all(), search)


@app.post("/", response_model=ClientOut, status_code=status.HTTP_201_CREATED)
def create_client(payload: ClientCreate, db: Session = Depends(get_db), _: User = Depends(require_clients_edit)):
    client = client_service.create_client(db, payload)
    db.commit()
    db.refresh(client)
    return client


@app.post("/intake", response_model=LeadIntakeOut)
async def intake_lead(request: Request, db: Session = Depends(get_db)):
    """Заявка с сайта (0111-a) — единственная ручка раздела без JWT.

    Токен — заголовок `X-Intake-Token` (или `?token=` / поле `token` формы: вебхук
    Tilda не умеет свои заголовки). Тело — JSON или обычная форма
    (`application/x-www-form-urlencoded`), имена полей без учёта регистра:
    `name`, `phone`, `email`, `message`, `source`, остальные `utm_*` попадают в
    `utm`. 200 `created` — заведён новый лид, 200 `duplicate` — телефон/почта уже
    есть у клиента, к нему добавлено повторное обращение."""
    intake_auth.check_token_configured()
    intake_auth.check_rate_limit(intake_auth.client_address(request))
    fields = await _intake_fields(request)
    intake_auth.check_token(
        request.headers.get("x-intake-token") or request.query_params.get("token") or fields.pop("token", None)
    )
    # Пробный запрос Tilda при сохранении вебхука (`test=test`, без контактов):
    # без 200 сервис не подключается. Токен уже проверен; клиента не создаём.
    if str(fields.get("test", "")).strip().lower() == "test" and not (fields.get("phone") or fields.get("email")):
        return JSONResponse({"status": "test"})
    try:
        payload = LeadIntakePayload.model_validate(_intake_payload(fields))
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=[{"loc": e["loc"], "msg": e["msg"]} for e in exc.errors()],
        )
    client, created = client_service.intake_lead(db, payload)
    db.commit()
    # Уведомление — после commit (notify коммитит сам); сбой уведомления не должен
    # терять заявку, которая уже сохранена.
    try:
        client_service.notify_new_lead(db, client, created)
    except Exception:  # noqa: BLE001
        logger.exception("intake: не удалось отправить уведомление о заявке клиента %s", client.id)
    return LeadIntakeOut(status="created" if created else "duplicate", client_id=client.id)


async def _intake_fields(request: Request) -> dict:
    """Плоский словарь полей из JSON или формы; ключи — в нижнем регистре."""
    try:
        if "json" in request.headers.get("content-type", ""):
            raw = await request.json()
            if not isinstance(raw, dict):
                raise ValueError
        elif "multipart" in request.headers.get("content-type", ""):
            raw = dict((await request.form()).items())
        else:
            # urlencoded разбираем сами как UTF-8: штатный разбор Starlette читает
            # непроцентованные байты как latin-1 и ломает кириллицу.
            raw = dict(parse_qsl((await request.body()).decode("utf-8", errors="replace"), keep_blank_values=True))
    except Exception:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Не удалось прочитать тело заявки")
    return {str(k).strip().lower(): v for k, v in raw.items()}


# Поля Tilda и формы, которые не несут сути обращения: служебные и согласие.
_INTAKE_SKIP = {
    "token", "name", "phone", "email", "message", "source", "utm", "textarea", "comments", "comment",
    "checkbox", "tranid", "formid", "formname", "forma", "formservices[]", "cookies", "referer",
}


def _intake_payload(fields: dict) -> dict:
    def text(key: str) -> str | None:
        value = fields.get(key)
        return value if isinstance(value, str) else None

    utm = {k: v[:255] for k, v in fields.items() if k.startswith("utm_") and isinstance(v, str) and v.strip()}
    if isinstance(fields.get("utm"), dict):
        utm.update({str(k): str(v)[:255] for k, v in fields["utm"].items()})
    data = {key: text(key) for key in ("name", "phone", "email", "message", "source")}
    data = {k: v for k, v in data.items() if v is not None}
    # Форма Tilda называет поле текста «Textarea» (имя переменной блока), в других
    # формах — comments/comment; всё это — текст обращения. Прочие значимые поля
    # (тип дома, площадь и т.п. из формы расчёта) дописываются к тексту «ключ: значение».
    if not data.get("message"):
        data["message"] = next((v.strip() for k in ("textarea", "comments", "comment") if (v := text(k)) and v.strip()), "")
    extras = [
        f"{key}: {value.strip()}"
        for key, value in fields.items()
        if key not in _INTAKE_SKIP and not key.startswith("utm_") and isinstance(value, str) and value.strip()
    ]
    if extras:
        data["message"] = "\n".join(filter(None, [data.get("message", ""), *extras]))
    data["message"] = data.get("message", "")[:4000]
    return {**data, "utm": utm or None}


@app.get("/assignees", response_model=list[ClientManagerOut])
def list_assignees(db: Session = Depends(get_db), _: User = Depends(require_clients_edit)):
    """Сотрудники, которым можно поставить задачу по клиенту (0111-c): активные
    пользователи с доступом к разделу «Клиенты». Полный список пользователей
    (`GET /api/auth/users`) отдаётся только админу, а ставить задачи на коллег
    должны и обычные сотрудники с правом редактирования клиентов. Объявлена до
    `/{client_id}`."""
    users = db.query(User).filter(User.is_active.is_(True)).order_by(User.full_name, User.id).all()
    return [user for user in users if user.has_access(Module.CLIENTS)]


@app.get("/{client_id}", response_model=ClientOut)
def get_client(client_id: int, db: Session = Depends(get_db), _: User = Depends(require_clients_view)):
    return client_service.get_client_or_404(db, client_id)


@app.patch("/{client_id}/basic", response_model=ClientOut)
def update_basic(
    client_id: int,
    payload: ClientBasicUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_clients_edit),
):
    """Дозаполнить базовые данные клиента: ФИО, телефон, почта, способы связи
    (0111-b). 409 — телефон/почта уже у другого клиента, 422 — пустое ФИО или
    нет ни телефона, ни почты."""
    client = client_service.get_client_or_404(db, client_id)
    client = client_service.update_basic(db, client, payload, user)
    db.commit()
    db.refresh(client)
    return client


@app.post("/{client_id}/take", response_model=ClientOut)
def take_lead(client_id: int, db: Session = Depends(get_db), _: User = Depends(require_clients_edit)):
    """«Взять в работу»: снять подсветку необработанной заявки с сайта (0111-b)."""
    client = client_service.get_client_or_404(db, client_id)
    client = client_service.take_lead(db, client)
    db.commit()
    db.refresh(client)
    return client


@app.patch("/{client_id}/source", response_model=ClientOut)
def update_source(
    client_id: int,
    payload: ClientSourceUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_clients_edit),
):
    """Кто привёл клиента — сам пришёл или агентство-партнёр (0079-c)."""
    client = client_service.get_client_or_404(db, client_id)
    client = client_service.update_source(db, client, payload)
    db.commit()
    db.refresh(client)
    return client


@app.patch("/{client_id}/documents", response_model=ClientOut)
def update_documents(
    client_id: int,
    payload: ClientDocumentsUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_clients_edit),
):
    client = client_service.get_client_or_404(db, client_id)
    client = client_service.update_documents(db, client, payload)
    db.commit()
    db.refresh(client)
    return client


@app.patch("/{client_id}/manager", response_model=ClientOut)
def update_manager(
    client_id: int,
    payload: ClientManagerUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_clients_edit),
):
    """Назначить/сменить/снять ответственного менеджера (0080-a) — те же
    права, что на смену стадии клиента."""
    client = client_service.get_client_or_404(db, client_id)
    client = client_service.update_manager(db, client, payload)
    db.commit()
    db.refresh(client)
    return client


@app.patch("/{client_id}/houses-count", response_model=ClientOut)
def update_houses_count(
    client_id: int,
    payload: ClientHousesCountUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_clients_edit),
):
    client = client_service.get_client_or_404(db, client_id)
    client = client_service.update_houses_count(db, client, payload)
    db.commit()
    db.refresh(client)
    return client


@app.patch("/{client_id}/payment", response_model=ClientOut)
def update_payment(
    client_id: int,
    payload: ClientPaymentUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_clients_edit),
):
    client = client_service.get_client_or_404(db, client_id)
    client = client_service.update_payment(db, client, payload, current_user.id)
    db.commit()
    db.refresh(client)
    return client


@app.patch("/{client_id}/payment-edit-unlock", response_model=ClientOut)
def set_payment_edit_unlock(
    client_id: int,
    payload: ClientPaymentEditUnlockUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    client = client_service.get_client_or_404(db, client_id)
    client = client_service.set_payment_edit_unlocked(db, client, payload.unlocked)
    db.commit()
    db.refresh(client)
    return client


@app.patch("/{client_id}/balance-payment", response_model=ClientOut)
def record_balance_payment(
    client_id: int,
    payload: ClientBalancePaymentUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_clients_edit),
):
    client = client_service.get_client_or_404(db, client_id)
    client = client_service.record_balance_payment(db, client, payload, current_user.id)
    db.commit()
    db.refresh(client)
    return client


@app.patch("/{client_id}/balance-due-date", response_model=ClientOut)
def update_balance_due_date(
    client_id: int,
    payload: ClientBalanceDueDateUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_clients_edit),
):
    """Срок оплаты остатка по договору (0084-j) — те же права, что на формат
    расчёта; дедлайн задачи приёма остатка следует за ним."""
    client = client_service.get_client_or_404(db, client_id)
    client = client_service.update_balance_due_date(db, client, payload, current_user)
    db.commit()
    db.refresh(client)
    return client


@app.post("/{client_id}/chat-links", response_model=ClientChatLinkOut, status_code=status.HTTP_201_CREATED)
def create_chat_link(
    client_id: int,
    payload: ClientChatLinkCreate,
    db: Session = Depends(get_db),
    _: User = Depends(require_clients_edit),
):
    client = client_service.get_client_or_404(db, client_id)
    link = client_service.create_chat_link(db, client, payload)
    db.commit()
    db.refresh(link)
    return link


@app.patch("/{client_id}/chat-links/{link_id}", response_model=ClientChatLinkOut)
def update_chat_link(
    client_id: int,
    link_id: int,
    payload: ClientChatLinkUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_clients_edit),
):
    link = client_service.get_chat_link_or_404(db, client_id, link_id)
    link = client_service.update_chat_link(db, link, payload)
    db.commit()
    db.refresh(link)
    return link


@app.delete("/{client_id}/chat-links/{link_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_chat_link(
    client_id: int,
    link_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_clients_edit),
):
    link = client_service.get_chat_link_or_404(db, client_id, link_id)
    client_service.delete_chat_link(db, link)
    db.commit()


@app.post("/{client_id}/contract-file", response_model=ClientOut)
def upload_contract_files(
    client_id: int,
    contract: UploadFile,
    appendix: UploadFile,
    db: Session = Depends(get_db),
    user: User = Depends(require_clients_edit),
):
    """Договор и приложение к договору — одно действие (0061): нельзя
    загрузить один без другого."""
    client = client_service.get_client_or_404(db, client_id)
    contract_asset = save_upload_file(db, contract, FilePurpose.CONTRACT, user)
    appendix_asset = save_upload_file(db, appendix, FilePurpose.CONTRACT_APPENDIX, user)
    client = client_service.set_contract_files(db, client, contract_asset.id, appendix_asset.id)
    db.commit()
    db.refresh(client)
    return client


@app.post("/{client_id}/contract/verify", response_model=ClientOut)
def verify_contract(
    client_id: int,
    payload: ClientContractVerify,
    db: Session = Depends(get_db),
    user: User = Depends(require_clients_edit),
):
    """Отметить договор или приложение проверенным (0084-i): загрузка файла
    проверкой не считается. Права — те же, что на загрузку."""
    client = client_service.get_client_or_404(db, client_id)
    client = client_service.verify_contract_document(db, client, payload, user)
    db.commit()
    db.refresh(client)
    return client


@app.post("/{client_id}/house-project-file", response_model=ClientOut)
def upload_house_project_file(
    client_id: int,
    file: UploadFile,
    db: Session = Depends(get_db),
    user: User = Depends(require_clients_edit),
):
    client = client_service.get_client_or_404(db, client_id)
    asset = save_upload_file(db, file, FilePurpose.HOUSE_PROJECT, user)
    client = client_service.set_house_project_file(db, client, asset.id)
    db.commit()
    db.refresh(client)
    return client


@app.post("/{client_id}/ar-file", response_model=ClientOut)
def upload_ar_file(
    client_id: int,
    file: UploadFile,
    db: Session = Depends(get_db),
    user: User = Depends(require_clients_edit),
):
    client = client_service.get_client_or_404(db, client_id)
    asset = save_upload_file(db, file, FilePurpose.ARCHITECTURAL_DECISIONS, user)
    client = client_service.set_ar_file(db, client, asset.id)
    db.commit()
    db.refresh(client)
    return client


@app.post("/{client_id}/kr-file", response_model=ClientOut)
def upload_kr_file(
    client_id: int,
    file: UploadFile,
    db: Session = Depends(get_db),
    user: User = Depends(require_clients_edit),
):
    client = client_service.get_client_or_404(db, client_id)
    asset = save_upload_file(db, file, FilePurpose.CONSTRUCTIVE_DECISIONS, user)
    client = client_service.set_kr_file(db, client, asset.id)
    db.commit()
    db.refresh(client)
    return client


@app.post("/{client_id}/notes", response_model=ClientNoteOut, status_code=status.HTTP_201_CREATED)
def add_note(
    client_id: int,
    payload: ClientNoteCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_clients_edit),
):
    client = client_service.get_client_or_404(db, client_id)
    note = client_service.add_note(db, client, user.id, payload.text)
    db.commit()
    db.refresh(note)
    return note


@app.patch("/{client_id}/notes/{note_id}", response_model=ClientNoteOut)
def update_note(
    client_id: int,
    note_id: int,
    payload: ClientNoteUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_clients_edit),
):
    note = db.get(ClientNote, note_id)
    if not note or note.client_id != client_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Заметка не найдена")
    note = client_service.update_note(db, note, payload.text)
    db.commit()
    db.refresh(note)
    return note


@app.delete("/{client_id}/notes/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_note(
    client_id: int,
    note_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_clients_full),
):
    note = db.get(ClientNote, note_id)
    if not note or note.client_id != client_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Заметка не найдена")
    client_service.delete_note(db, note)
    db.commit()


@app.delete("/{client_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_client(client_id: int, db: Session = Depends(get_db), _: User = Depends(require_clients_full)):
    client = client_service.get_client_or_404(db, client_id)
    client_service.delete_client(db, client)
    db.commit()


@app.post("/reconcile-stage-tasks")
def reconcile_stage_tasks(db: Session = Depends(get_db), _: User = Depends(require_clients_full)):
    """Ручной прогон сверки задач смены стадии с реальностью (та же, что раз в
    час фоном). Создаёт недостающие задачи, закрывает устаревшие и дубли,
    обновляет заголовок и описание открытых задач до актуального текста."""
    report = client_reconcile.reconcile_client_stage_tasks(db)
    db.commit()
    return report


@app.get("/{client_id}/tasks", response_model=list[ClientTaskOut])
def list_client_tasks(
    client_id: int, db: Session = Depends(get_db), _: User = Depends(require_clients_view)
):
    """Задачи менеджера по клиенту (0079-d) — и открытые, и закрытые."""
    client = client_service.get_client_or_404(db, client_id)
    return [ClientTaskOut.from_task(task) for task in client.followup_tasks]


@app.post("/{client_id}/tasks", response_model=ClientTaskOut, status_code=status.HTTP_201_CREATED)
def create_client_task(
    client_id: int,
    payload: ClientTaskCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_clients_edit),
):
    client = client_service.get_client_or_404(db, client_id)
    task = client_service.create_followup_task(db, client, payload, user)
    db.commit()
    db.refresh(task)
    return ClientTaskOut.from_task(task)


@app.post("/{client_id}/tasks/{task_id}/deadline", response_model=ClientTaskOut)
def shift_client_task_deadline(
    client_id: int,
    task_id: int,
    payload: ClientTaskDeadlineUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_clients_edit),
):
    """Перенести срок задачи с причиной — причина уходит в журнал задачи."""
    client = client_service.get_client_or_404(db, client_id)
    task = client_service.get_followup_task_or_404(db, client, task_id)
    task = client_service.shift_followup_deadline(db, task, payload, user)
    db.commit()
    db.refresh(task)
    return ClientTaskOut.from_task(task)


@app.post("/{client_id}/tasks/{task_id}/close", response_model=ClientTaskOut)
def close_client_task(
    client_id: int,
    task_id: int,
    payload: ClientTaskClose,
    db: Session = Depends(get_db),
    user: User = Depends(require_clients_edit),
):
    """Закрыть задачу описанием решения и, если передана, сразу завести
    вытекающую."""
    client = client_service.get_client_or_404(db, client_id)
    task = client_service.get_followup_task_or_404(db, client, task_id)
    task = client_service.close_followup_task(db, client, task, payload, user)
    db.commit()
    db.refresh(task)
    return ClientTaskOut.from_task(task)


@app.post("/{client_id}/transition", response_model=ClientOut)
def transition_client(client_id: int, db: Session = Depends(get_db), _: User = Depends(require_clients_edit)):
    client = client_service.get_client_or_404(db, client_id)
    client = client_service.transition_stage(db, client)
    db.commit()
    db.refresh(client)
    return client
