from fastapi import Depends, FastAPI
from sqlalchemy.orm import Session

from app.common.module_access import Module as AccessModule
from app.core.deps import require_module
from app.db.session import get_db
from app.installation import service as installation_service
from app.installation.schemas import InstallationOut, InstallationUpdate
from app.users.models import User

app = FastAPI(
    title="Soborbum — Монтаж",
    description="Доставка, установка и проработка дома на месте.",
    version="0.2.1",
)

require_installation = require_module(AccessModule.INSTALLATION)


@app.post("/start/{cycle_id}", response_model=InstallationOut, status_code=201)
def start_installation(cycle_id: int, db: Session = Depends(get_db), _: User = Depends(require_installation)):
    installation = installation_service.start_installation(db, cycle_id)
    db.commit()
    db.refresh(installation)
    return installation


@app.get("/{installation_id}", response_model=InstallationOut)
def get_installation(installation_id: int, db: Session = Depends(get_db), _: User = Depends(require_installation)):
    return installation_service.get_installation_or_404(db, installation_id)


@app.patch("/{installation_id}", response_model=InstallationOut)
def update_installation(
    installation_id: int,
    payload: InstallationUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_installation),
):
    installation = installation_service.get_installation_or_404(db, installation_id)
    installation = installation_service.update_installation(db, installation, payload)
    db.commit()
    db.refresh(installation)
    return installation


@app.post("/{installation_id}/transition", response_model=InstallationOut)
def transition_installation(
    installation_id: int, db: Session = Depends(get_db), _: User = Depends(require_installation)
):
    installation = installation_service.get_installation_or_404(db, installation_id)
    installation = installation_service.transition_stage(db, installation)
    db.commit()
    db.refresh(installation)
    return installation


@app.post("/{installation_id}/complete", response_model=InstallationOut)
def complete_installation(
    installation_id: int, db: Session = Depends(get_db), _: User = Depends(require_installation)
):
    installation = installation_service.get_installation_or_404(db, installation_id)
    installation = installation_service.complete_installation(db, installation)
    db.commit()
    db.refresh(installation)
    return installation
