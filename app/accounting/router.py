from datetime import datetime

from fastapi import Depends, FastAPI, Query, status
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.accounting import service as accounting_service
from app.accounting.models import (
    MoneyDirection,
    MoneyMovementStatus,
    MoneySourceKind,
    MoneySubkind,
)
from app.accounting.schemas import (
    MoneyMovementCreate,
    MoneyMovementOut,
    MoneyMovementStatusChange,
    MoneyMovementUpdate,
)
from app.common.module_access import Module as AccessModule
from app.core.deps import require_module
from app.db.session import get_db
from app.users.models import User

app = FastAPI(
    title="Soborbum — Бухгалтерия",
    description=(
        "Единый реестр движения денежных средств. Модель и терминология "
        "повторяют МойСклад (см. docs/moysklad-accounting-research.md). "
        "Сшивку с «Состоянием оплаты» клиента, зарплатой и оплатой поставки "
        "делает задача 0011-f — здесь только реестр и статусная машина."
    ),
    version="0.1.0",
)

require_accounting = require_module(AccessModule.ACCOUNTING)


@app.get("/money-movements/enums")
def money_movement_enums(_: User = Depends(require_accounting)):
    """Справочники для фронта: виды, статусы, оценки, типы источников."""
    return {
        "direction": [e.value for e in MoneyDirection],
        "subkind": [e.value for e in MoneySubkind],
        "status": [e.value for e in MoneyMovementStatus],
        "source_kind": [e.value for e in MoneySourceKind],
    }


@app.get("/money-movements", response_model=list[MoneyMovementOut])
def list_money_movements(
    db: Session = Depends(get_db),
    _: User = Depends(require_accounting),
    direction: MoneyDirection | None = None,
    subkind: MoneySubkind | None = None,
    status_filter: MoneyMovementStatus | None = Query(None, alias="status"),
    source_kind: MoneySourceKind | None = None,
    client_id: int | None = None,
    employee_id: int | None = None,
    supply_id: int | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    movements = accounting_service.list_money_movements(
        db,
        direction=direction,
        subkind=subkind,
        status_=status_filter,
        source_kind=source_kind,
        client_id=client_id,
        employee_id=employee_id,
        supply_id=supply_id,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )
    return [MoneyMovementOut.from_movement(mm) for mm in movements]


@app.post("/money-movements", response_model=MoneyMovementOut, status_code=201)
def create_money_movement(
    payload: MoneyMovementCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_accounting),
):
    mm = accounting_service.create_money_movement(
        db, payload, initiator_id=payload.initiator_id or user.id
    )
    return MoneyMovementOut.from_movement(mm)


@app.get("/money-movements/{mm_id}", response_model=MoneyMovementOut)
def get_money_movement(
    mm_id: int, db: Session = Depends(get_db), _: User = Depends(require_accounting)
):
    return MoneyMovementOut.from_movement(accounting_service.get_money_movement(db, mm_id))


@app.patch("/money-movements/{mm_id}", response_model=MoneyMovementOut)
def update_money_movement(
    mm_id: int,
    payload: MoneyMovementUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_accounting),
):
    mm = accounting_service.get_money_movement(db, mm_id)
    mm = accounting_service.update_money_movement(db, mm, payload)
    return MoneyMovementOut.from_movement(mm)


@app.delete("/money-movements/{mm_id}", status_code=204)
def delete_money_movement(
    mm_id: int, db: Session = Depends(get_db), _: User = Depends(require_accounting)
):
    mm = accounting_service.get_money_movement(db, mm_id)
    accounting_service.delete_money_movement(db, mm)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/money-movements/{mm_id}/status", response_model=MoneyMovementOut)
def change_money_movement_status(
    mm_id: int,
    payload: MoneyMovementStatusChange,
    db: Session = Depends(get_db),
    _: User = Depends(require_accounting),
):
    mm = accounting_service.get_money_movement(db, mm_id)
    mm = accounting_service.change_status(db, mm, payload.to, payload.reason)
    return MoneyMovementOut.from_movement(mm)
