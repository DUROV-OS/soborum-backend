from fastapi import Depends, FastAPI, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.clients import reconcile as client_reconcile
from app.clients import service as client_service
from app.clients.models import Client, ClientNote, ClientStage
from app.clients.schemas import (
    ClientBalancePaymentUpdate,
    ClientChatLinkCreate,
    ClientChatLinkOut,
    ClientChatLinkUpdate,
    ClientCreate,
    ClientDocumentsUpdate,
    ClientHousesCountUpdate,
    ClientNoteCreate,
    ClientNoteOut,
    ClientNoteUpdate,
    ClientOut,
    ClientPaymentEditUnlockUpdate,
    ClientPaymentUpdate,
)
from app.common.files import FilePurpose, save_upload_file
from app.common.module_access import Module
from app.core.deps import require_admin, require_edit, require_full, require_view
from app.db.session import get_db
from app.users.models import User

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
):
    query = db.query(Client)
    if stage is not None:
        query = query.filter(Client.stage == stage)
    return query.order_by(Client.id.desc()).all()


@app.post("/", response_model=ClientOut, status_code=status.HTTP_201_CREATED)
def create_client(payload: ClientCreate, db: Session = Depends(get_db), _: User = Depends(require_clients_edit)):
    client = client_service.create_client(db, payload)
    db.commit()
    db.refresh(client)
    return client


@app.get("/{client_id}", response_model=ClientOut)
def get_client(client_id: int, db: Session = Depends(get_db), _: User = Depends(require_clients_view)):
    return client_service.get_client_or_404(db, client_id)


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
    час фоном). Создаёт недостающие задачи, закрывает устаревшие и дубли."""
    report = client_reconcile.reconcile_client_stage_tasks(db)
    db.commit()
    return report


@app.post("/{client_id}/transition", response_model=ClientOut)
def transition_client(client_id: int, db: Session = Depends(get_db), _: User = Depends(require_clients_edit)):
    client = client_service.get_client_or_404(db, client_id)
    client = client_service.transition_stage(db, client)
    db.commit()
    db.refresh(client)
    return client
