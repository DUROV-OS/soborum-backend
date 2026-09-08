from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.clients.models import PaymentPlan
from app.cycle.models import Cycle, CycleStatus
from app.installation.models import INSTALLATION_STAGE_ORDER, Installation, InstallationStage
from app.installation.schemas import InstallationUpdate


def get_installation_or_404(db: Session, installation_id: int) -> Installation:
    installation = db.get(Installation, installation_id)
    if not installation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Монтаж не найден")
    return installation


def start_installation(db: Session, cycle_id: int) -> Installation:
    cycle = db.get(Cycle, cycle_id)
    if not cycle:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Цикл не найден")
    if cycle.production is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="У цикла ещё нет производства")
    if cycle.installation is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Монтаж для этого цикла уже начат")

    installation = Installation(
        cycle_id=cycle_id,
        stage=InstallationStage.DELIVERY,
        address=cycle.client.installation_address if cycle.client else None,
    )
    db.add(installation)
    cycle.status = CycleStatus.INSTALLATION
    db.flush()
    return installation


def update_installation(db: Session, installation: Installation, payload: InstallationUpdate) -> Installation:
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(installation, field, value)
    db.flush()
    return installation


def transition_stage(db: Session, installation: Installation) -> Installation:
    idx = INSTALLATION_STAGE_ORDER.index(installation.stage)
    if idx + 1 >= len(INSTALLATION_STAGE_ORDER):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Монтаж уже на последней стадии")
    next_stage = INSTALLATION_STAGE_ORDER[idx + 1]
    installation.stage = next_stage
    db.flush()

    if next_stage == InstallationStage.FOLLOWUP:
        pass  # last stage reached; cycle is marked COMPLETED once followup itself finishes (see below)

    return installation


def complete_installation(db: Session, installation: Installation) -> Installation:
    if installation.stage != InstallationStage.FOLLOWUP:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Завершить можно только монтаж на стадии «проработка»"
        )
    # Для планов с оплатой после получения дома цикл нельзя закрыть, пока
    # клиент не погасил остаток (см. app.clients.models.PaymentPlan).
    client = installation.cycle.client
    if client and client.payment_plan != PaymentPlan.FULL_PREPAYMENT and not client.balance_paid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Клиент ещё не внёс оплату после получения — цикл нельзя завершить",
        )
    installation.cycle.status = CycleStatus.COMPLETED
    db.flush()
    return installation
