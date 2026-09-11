"""Бизнес-логика реестра движения денег: инварианты привязки к источнику и
статусная машина `draft → approved → posted → cancelled`.

Проекция на МойСклад (см. `backend/docs/moysklad-accounting-research.md`):
`posted` ⇔ «проведён» (`applicable = true`) — деньги и взаиморасчёты учтены,
запись неизменяема; шаги `draft`/`approved` — процессный слой поверх `state`.
"""

from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.accounting.models import (
    EXPENSE_SUBKINDS,
    INCOME_SUBKINDS,
    SUBKIND_REQUIRED_SOURCE,
    MoneyDirection,
    MoneyMovement,
    MoneyMovementStatus,
    MoneySourceKind,
    MoneySubkind,
)
from app.accounting.schemas import MoneyMovementCreate, MoneyMovementUpdate
from app.clients.models import Client
from app.users.models import User
from app.warehouse.models import Supply

# Разрешённые переходы статуса — только вперёд + отмена из любого «живого».
_ALLOWED_TRANSITIONS: dict[MoneyMovementStatus, set[MoneyMovementStatus]] = {
    MoneyMovementStatus.DRAFT: {MoneyMovementStatus.APPROVED, MoneyMovementStatus.CANCELLED},
    MoneyMovementStatus.APPROVED: {MoneyMovementStatus.POSTED, MoneyMovementStatus.CANCELLED},
    MoneyMovementStatus.POSTED: {MoneyMovementStatus.CANCELLED},
    MoneyMovementStatus.CANCELLED: set(),
}

_EDITABLE_STATUSES = {MoneyMovementStatus.DRAFT, MoneyMovementStatus.APPROVED}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _direction_for(subkind: MoneySubkind) -> MoneyDirection:
    return MoneyDirection.INCOME if subkind in INCOME_SUBKINDS else MoneyDirection.EXPENSE


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _resolve_source(
    db: Session,
    subkind: MoneySubkind,
    client_id: int | None,
    employee_id: int | None,
    supply_id: int | None,
) -> MoneySourceKind:
    """Проверяет инварианты полиморфной привязки и возвращает согласованный
    `source_kind`. Привязка — не более чем к одному источнику; подвиды
    sale_income / salary_payout / supply_payment требуют «свой» источник,
    остальные — не допускают ни одного."""

    provided = [
        (MoneySourceKind.CLIENT, client_id),
        (MoneySourceKind.EMPLOYEE, employee_id),
        (MoneySourceKind.SUPPLY, supply_id),
    ]
    set_kinds = [kind for kind, value in provided if value is not None]
    if len(set_kinds) > 1:
        raise _bad_request("Проводку нельзя привязать больше чем к одному источнику")

    required = SUBKIND_REQUIRED_SOURCE.get(subkind)
    actual = set_kinds[0] if set_kinds else MoneySourceKind.NONE

    if required is None:
        if actual is not MoneySourceKind.NONE:
            raise _bad_request(f"Подвид «{subkind.value}» не допускает привязку к источнику")
        return MoneySourceKind.NONE

    if actual is not required:
        raise _bad_request(
            f"Подвид «{subkind.value}» требует привязку к источнику «{required.value}»"
        )

    _assert_source_exists(db, required, client_id, employee_id, supply_id)
    return required


def _assert_source_exists(
    db: Session,
    kind: MoneySourceKind,
    client_id: int | None,
    employee_id: int | None,
    supply_id: int | None,
) -> None:
    if kind is MoneySourceKind.CLIENT and db.get(Client, client_id) is None:
        raise _bad_request("Клиент-источник не найден")
    if kind is MoneySourceKind.EMPLOYEE and db.get(User, employee_id) is None:
        raise _bad_request("Сотрудник-источник не найден")
    if kind is MoneySourceKind.SUPPLY and db.get(Supply, supply_id) is None:
        raise _bad_request("Поставка-источник не найдена")


def create_money_movement(
    db: Session, data: MoneyMovementCreate, initiator_id: int
) -> MoneyMovement:
    if data.amount is None or data.amount <= 0:
        raise _bad_request("Сумма проводки должна быть положительной")
    if data.tax is not None and data.tax < 0:
        raise _bad_request("Сумма налога не может быть отрицательной")

    source_kind = _resolve_source(
        db, data.subkind, data.client_id, data.employee_id, data.supply_id
    )

    mm = MoneyMovement(
        direction=_direction_for(data.subkind),
        subkind=data.subkind,
        amount=data.amount,
        currency=data.currency or "RUB",
        tax=data.tax or 0,
        assessment=data.assessment,
        affects_profit=data.affects_profit,
        initiator_id=initiator_id,
        status=MoneyMovementStatus.DRAFT,
        doc_date=data.doc_date,
        payment_purpose=data.payment_purpose,
        comment=data.comment,
        external_number=data.external_number,
        source_kind=source_kind,
        client_id=data.client_id,
        employee_id=data.employee_id,
        supply_id=data.supply_id,
    )
    db.add(mm)
    db.commit()
    db.refresh(mm)
    return mm


def get_money_movement(db: Session, mm_id: int) -> MoneyMovement:
    mm = db.get(MoneyMovement, mm_id)
    if mm is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Проводка не найдена")
    return mm


def list_money_movements(
    db: Session,
    *,
    direction: MoneyDirection | None = None,
    subkind: MoneySubkind | None = None,
    status_: MoneyMovementStatus | None = None,
    source_kind: MoneySourceKind | None = None,
    client_id: int | None = None,
    employee_id: int | None = None,
    supply_id: int | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[MoneyMovement]:
    stmt = select(MoneyMovement)
    if direction is not None:
        stmt = stmt.where(MoneyMovement.direction == direction)
    if subkind is not None:
        stmt = stmt.where(MoneyMovement.subkind == subkind)
    if status_ is not None:
        stmt = stmt.where(MoneyMovement.status == status_)
    if source_kind is not None:
        stmt = stmt.where(MoneyMovement.source_kind == source_kind)
    if client_id is not None:
        stmt = stmt.where(MoneyMovement.client_id == client_id)
    if employee_id is not None:
        stmt = stmt.where(MoneyMovement.employee_id == employee_id)
    if supply_id is not None:
        stmt = stmt.where(MoneyMovement.supply_id == supply_id)
    # Период — по дате платёжного документа (импорт выпиской), иначе по дате
    # проведения, иначе по созданию.
    effective_date = func.coalesce(
        MoneyMovement.doc_date, MoneyMovement.posted_at, MoneyMovement.created_at
    )
    if date_from is not None:
        stmt = stmt.where(effective_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(effective_date <= date_to)
    stmt = stmt.order_by(MoneyMovement.created_at.desc(), MoneyMovement.id.desc())
    stmt = stmt.limit(limit).offset(offset)
    return list(db.execute(stmt).scalars().all())


def update_money_movement(
    db: Session, mm: MoneyMovement, data: MoneyMovementUpdate
) -> MoneyMovement:
    if mm.status not in _EDITABLE_STATUSES:
        raise _conflict(
            f"Проводку в статусе «{mm.status.value}» редактировать нельзя"
        )

    payload = data.model_dump(exclude_unset=True)

    new_subkind = payload.get("subkind", mm.subkind)
    new_client = payload.get("client_id", mm.client_id)
    new_employee = payload.get("employee_id", mm.employee_id)
    new_supply = payload.get("supply_id", mm.supply_id)

    if "amount" in payload:
        if payload["amount"] is None or payload["amount"] <= 0:
            raise _bad_request("Сумма проводки должна быть положительной")
    if payload.get("tax") is not None and payload["tax"] < 0:
        raise _bad_request("Сумма налога не может быть отрицательной")

    source_kind = _resolve_source(db, new_subkind, new_client, new_employee, new_supply)

    for field in (
        "amount", "currency", "tax", "assessment", "affects_profit",
        "payment_purpose", "comment", "external_number",
        "subkind", "client_id", "employee_id", "supply_id",
    ):
        if field in payload:
            setattr(mm, field, payload[field])

    mm.direction = _direction_for(new_subkind)
    mm.source_kind = source_kind
    db.commit()
    db.refresh(mm)
    return mm


def delete_money_movement(db: Session, mm: MoneyMovement) -> None:
    if mm.status is not MoneyMovementStatus.DRAFT:
        raise _conflict(
            "Удалять можно только черновик; для остального — отмена с основанием"
        )
    db.delete(mm)
    db.commit()


def change_status(
    db: Session,
    mm: MoneyMovement,
    to: MoneyMovementStatus,
    reason: str | None = None,
) -> MoneyMovement:
    if to not in _ALLOWED_TRANSITIONS[mm.status]:
        raise _conflict(f"Недопустимый переход статуса «{mm.status.value}» → «{to.value}»")

    if to is MoneyMovementStatus.POSTED:
        if mm.amount is None or mm.amount <= 0:
            raise _conflict("Нельзя провести проводку без положительной суммы")
        if mm.subkind is None:
            raise _conflict("Нельзя провести проводку без вида")
        if mm.initiator_id is None:
            raise _conflict("Нельзя провести проводку без инициатора")
        mm.posted_at = _utcnow()

    if to is MoneyMovementStatus.CANCELLED:
        cleaned = (reason or "").strip()
        if not cleaned:
            raise _bad_request("Отмена проводки требует указания причины")
        mm.cancel_reason = cleaned

    mm.status = to
    db.commit()
    db.refresh(mm)
    return mm
