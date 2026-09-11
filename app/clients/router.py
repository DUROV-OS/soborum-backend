from fastapi import Depends, FastAPI, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.clients import reconcile as client_reconcile
from app.clients import service as client_service
from app.clients.models import Client, ClientNote, ClientStage
from app.clients.schemas import (
    ClientBalancePaymentUpdate,
    ClientCreate,
    ClientDocumentsUpdate,
    ClientMaxChatUpdate,
    ClientNoteCreate,
    ClientNoteOut,
    ClientNoteUpdate,
    ClientOut,
    ClientPaymentUpdate,
    ClientProjectUpdate,
)
from app.common.files import FilePurpose, save_upload_file
from app.common.module_access import Module
from app.core.deps import require_admin, require_module
from app.db.session import get_db
from app.users.models import User

app = FastAPI(
    title="Soborbum — Клиенты",
    description="Клиенты от лида до постоплаты: базовые, проектные, "
    "документные данные, формат расчёта, оплата, привязка к чату MAX и заметки.",
    version="0.6.0",
)

require_clients = require_module(Module.CLIENTS)


@app.get("/", response_model=list[ClientOut])
def list_clients(
    db: Session = Depends(get_db),
    _: User = Depends(require_clients),
    stage: ClientStage | None = None,
):
    query = db.query(Client)
    if stage is not None:
        query = query.filter(Client.stage == stage)
    return query.order_by(Client.id.desc()).all()


@app.post("/", response_model=ClientOut, status_code=status.HTTP_201_CREATED)
def create_client(payload: ClientCreate, db: Session = Depends(get_db), _: User = Depends(require_clients)):
    client = client_service.create_client(db, payload)
    db.commit()
    db.refresh(client)
    return client


@app.get("/{client_id}", response_model=ClientOut)
def get_client(client_id: int, db: Session = Depends(get_db), _: User = Depends(require_clients)):
    return client_service.get_client_or_404(db, client_id)


@app.patch("/{client_id}/project", response_model=ClientOut)
def update_project(
    client_id: int,
    payload: ClientProjectUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_clients),
):
    client = client_service.get_client_or_404(db, client_id)
    client = client_service.update_project(db, client, payload)
    db.commit()
    db.refresh(client)
    return client


@app.patch("/{client_id}/documents", response_model=ClientOut)
def update_documents(
    client_id: int,
    payload: ClientDocumentsUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_clients),
):
    client = client_service.get_client_or_404(db, client_id)
    client = client_service.update_documents(db, client, payload)
    db.commit()
    db.refresh(client)
    return client


@app.patch("/{client_id}/payment", response_model=ClientOut)
def update_payment(
    client_id: int,
    payload: ClientPaymentUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_clients),
):
    client = client_service.get_client_or_404(db, client_id)
    client = client_service.update_payment(db, client, payload)
    db.commit()
    db.refresh(client)
    return client


@app.patch("/{client_id}/balance-payment", response_model=ClientOut)
def record_balance_payment(
    client_id: int,
    payload: ClientBalancePaymentUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_clients),
):
    client = client_service.get_client_or_404(db, client_id)
    client = client_service.record_balance_payment(db, client, payload)
    db.commit()
    db.refresh(client)
    return client


@app.patch("/{client_id}/max-chat", response_model=ClientOut)
def set_max_chat(
    client_id: int,
    payload: ClientMaxChatUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_clients),
):
    client = client_service.get_client_or_404(db, client_id)
    client = client_service.set_max_chat_id(db, client, payload)
    db.commit()
    db.refresh(client)
    return client


@app.post("/{client_id}/contract-file", response_model=ClientOut)
def upload_contract_file(
    client_id: int,
    file: UploadFile,
    db: Session = Depends(get_db),
    user: User = Depends(require_clients),
):
    client = client_service.get_client_or_404(db, client_id)
    asset = save_upload_file(db, file, FilePurpose.CONTRACT, user)
    client = client_service.set_contract_file(db, client, asset.id)
    db.commit()
    db.refresh(client)
    return client


@app.post("/{client_id}/house-project-file", response_model=ClientOut)
def upload_house_project_file(
    client_id: int,
    file: UploadFile,
    db: Session = Depends(get_db),
    user: User = Depends(require_clients),
):
    client = client_service.get_client_or_404(db, client_id)
    asset = save_upload_file(db, file, FilePurpose.HOUSE_PROJECT, user)
    client = client_service.set_house_project_file(db, client, asset.id)
    db.commit()
    db.refresh(client)
    return client


@app.post("/{client_id}/notes", response_model=ClientNoteOut, status_code=status.HTTP_201_CREATED)
def add_note(
    client_id: int,
    payload: ClientNoteCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_clients),
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
    _: User = Depends(require_clients),
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
    _: User = Depends(require_clients),
):
    note = db.get(ClientNote, note_id)
    if not note or note.client_id != client_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Заметка не найдена")
    client_service.delete_note(db, note)
    db.commit()


@app.delete("/{client_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_client(client_id: int, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    client = client_service.get_client_or_404(db, client_id)
    client_service.delete_client(db, client)
    db.commit()


@app.post("/reconcile-stage-tasks")
def reconcile_stage_tasks(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """Ручной прогон сверки задач смены стадии с реальностью (та же, что раз в
    час фоном). Создаёт недостающие задачи, закрывает устаревшие и дубли."""
    report = client_reconcile.reconcile_client_stage_tasks(db)
    db.commit()
    return report


@app.post("/{client_id}/transition", response_model=ClientOut)
def transition_client(client_id: int, db: Session = Depends(get_db), _: User = Depends(require_clients)):
    client = client_service.get_client_or_404(db, client_id)
    client = client_service.transition_stage(db, client)
    db.commit()
    db.refresh(client)
    return client
