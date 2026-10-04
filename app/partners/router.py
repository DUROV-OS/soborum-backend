from fastapi import Depends, FastAPI, HTTPException, status
from sqlalchemy.orm import Session

from app.common.module_access import Module
from app.core.deps import require_edit, require_full, require_view
from app.db.session import get_db
from app.partners import service as partner_service
from app.partners.models import PartnerCategory, PartnerNote
from app.partners.schemas import (
    PartnerCreate,
    PartnerNoteCreate,
    PartnerNoteOut,
    PartnerOut,
    PartnerUpdate,
)
from app.users.models import User

app = FastAPI(
    title="Soborbum — База партнёров",
    description="Партнёры компании: коммерция, агентства недвижимости, риэлторы, "
    "специалисты по земле — с городом, контактами и заметками о договорённостях.",
    version="0.1.0",
)

# Партнёров ведут те же люди, что и клиентов (продажи), поэтому отдельного
# раздела в матрице доступа нет — право берётся из «Клиентов» (0083).
require_partners_view = require_view(Module.CLIENTS)
require_partners_edit = require_edit(Module.CLIENTS)
require_partners_full = require_full(Module.CLIENTS)


@app.get("/", response_model=list[PartnerOut])
def list_partners(
    db: Session = Depends(get_db),
    _: User = Depends(require_partners_view),
    category: PartnerCategory | None = None,
    city: str | None = None,
    search: str | None = None,
):
    """`city` — точное совпадение без учёта регистра; `search` — по имени,
    организации и цифрам телефона."""
    return partner_service.list_partners(db, category=category, city=city, search=search)


@app.get("/cities", response_model=list[str])
def list_cities(db: Session = Depends(get_db), _: User = Depends(require_partners_view)):
    return partner_service.list_cities(db)


@app.post("/", response_model=PartnerOut, status_code=status.HTTP_201_CREATED)
def create_partner(payload: PartnerCreate, db: Session = Depends(get_db), user: User = Depends(require_partners_edit)):
    partner = partner_service.create_partner(db, payload, created_by_id=user.id)
    db.commit()
    db.refresh(partner)
    return partner


@app.get("/{partner_id}", response_model=PartnerOut)
def get_partner(partner_id: int, db: Session = Depends(get_db), _: User = Depends(require_partners_view)):
    return partner_service.get_partner_or_404(db, partner_id)


@app.patch("/{partner_id}", response_model=PartnerOut)
def update_partner(
    partner_id: int,
    payload: PartnerUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_partners_edit),
):
    partner = partner_service.get_partner_or_404(db, partner_id)
    partner = partner_service.update_partner(db, partner, payload)
    db.commit()
    db.refresh(partner)
    return partner


@app.delete("/{partner_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_partner(partner_id: int, db: Session = Depends(get_db), _: User = Depends(require_partners_full)):
    partner = partner_service.get_partner_or_404(db, partner_id)
    partner_service.delete_partner(db, partner)
    db.commit()


@app.post("/{partner_id}/notes", response_model=PartnerNoteOut, status_code=status.HTTP_201_CREATED)
def add_note(
    partner_id: int,
    payload: PartnerNoteCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_partners_edit),
):
    partner = partner_service.get_partner_or_404(db, partner_id)
    note = partner_service.add_note(db, partner, user.id, payload.text)
    db.commit()
    db.refresh(note)
    return note


@app.delete("/{partner_id}/notes/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_note(
    partner_id: int,
    note_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_partners_full),
):
    # Удаление заметки — полный доступ, как у заметок клиента.
    note = db.get(PartnerNote, note_id)
    if not note or note.partner_id != partner_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Заметка не найдена")
    partner_service.delete_note(db, note)
    db.commit()
