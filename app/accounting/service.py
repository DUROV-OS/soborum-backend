"""Бизнес-логика реестра движения денег: инварианты привязки к источнику и
статусная машина `draft → approved → posted → cancelled`.

Проекция на МойСклад (см. `backend/docs/moysklad-accounting-research.md`):
`posted` ⇔ «проведён» (`applicable = true`) — деньги и взаиморасчёты учтены,
запись неизменяема; шаги `draft`/`approved` — процессный слой поверх `state`.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.accounting import payment_import
from app.accounting.models import (
    EXPENSE_SUBKINDS,
    INCOME_SUBKINDS,
    MONEY_SUBKIND_LABELS,
    SUBKIND_REQUIRED_SOURCE,
    MoneyAssessment,
    MoneyDirection,
    MoneyMovement,
    MoneyMovementStatus,
    MoneySourceKind,
    MoneySubkind,
    SupplierOrder,
    SupplierOrderStatus,
)
from app.accounting.schemas import (
    EmployeeSalaryOverview,
    MoneyMovementCreate,
    MoneyMovementOut,
    MoneyMovementUpdate,
    SupplierOrderCreate,
    SupplierOrderUpdate,
)
from app.clients.models import Client
from app.common.module_access import Module as AccessModule
from app.core.config import settings
from app.tasks import service as task_service
from app.tasks.models import TaskLinkType
from app.users import service as user_service
from app.users.models import User
from app.warehouse import service as warehouse_service
from app.warehouse.models import Supplier

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
    if kind is MoneySourceKind.SUPPLY and db.get(SupplierOrder, supply_id) is None:
        raise _bad_request("Заказ у поставщика — источник не найден")


_OPEN_STATUSES = {MoneyMovementStatus.DRAFT, MoneyMovementStatus.APPROVED}


def _assert_no_open_salary_payout(db: Session, employee_id: int | None) -> None:
    """Раздел «Сотрудники» (0023) начисляет зарплату по одной незакрытой
    проводке за раз — не даёт скопить несколько черновиков/утверждённых
    начислений на одного сотрудника, пока предыдущее не проведено/отменено."""
    if employee_id is None:
        return
    has_open = (
        db.query(MoneyMovement.id)
        .filter(
            MoneyMovement.subkind == MoneySubkind.SALARY_PAYOUT,
            MoneyMovement.employee_id == employee_id,
            MoneyMovement.status.in_(_OPEN_STATUSES),
        )
        .first()
        is not None
    )
    if has_open:
        raise _conflict("У сотрудника уже есть незакрытая зарплатная проводка")


def _assert_no_open_supply_payment(db: Session, supply_id: int | None) -> None:
    """0011-f: заказ у поставщика оплачивается один раз — повторный `pay` на
    заказ, который уже оплачивается (draft/approved) или оплачен (posted),
    отклоняется. Отменённая (`cancelled`) проводка оплату не блокирует."""
    if supply_id is None:
        return
    has_active = (
        db.query(MoneyMovement.id)
        .filter(
            MoneyMovement.subkind == MoneySubkind.SUPPLY_PAYMENT,
            MoneyMovement.supply_id == supply_id,
            MoneyMovement.status != MoneyMovementStatus.CANCELLED,
        )
        .first()
        is not None
    )
    if has_active:
        raise _conflict("Заказ уже оплачивается или оплачен")


def _create_approval_task(db: Session, mm: MoneyMovement) -> None:
    """0011-f: любая проводка в `draft` (авто- или вручную из 0011-e) порождает
    задачу на согласование — закрывается в `change_status` при уходе из `draft`."""
    assignees = user_service.users_with_access(db, AccessModule.ACCOUNTING)
    label = MONEY_SUBKIND_LABELS.get(mm.subkind, mm.subkind.value)
    task_service.create_link_task(
        db,
        title=f"Согласовать проводку «{label}» на {mm.amount:,.0f} ₽".replace(",", " "),
        link_type=TaskLinkType.MONEY_MOVEMENT_APPROVAL,
        link_id=mm.id,
        assignees=assignees,
        link_meta={"subkind": mm.subkind.value, "amount": mm.amount},
    )


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

    if data.subkind is MoneySubkind.SALARY_PAYOUT:
        _assert_no_open_salary_payout(db, data.employee_id)

    if data.subkind is MoneySubkind.SUPPLY_PAYMENT:
        _assert_no_open_supply_payment(db, data.supply_id)

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
    _create_approval_task(db, mm)
    db.commit()
    return mm


def record_sale_income(
    db: Session, *, client_id: int, amount: float, initiator_id: int
) -> MoneyMovement:
    """0011-f: авто-проводка «доход от продажи» на переходе `is_paid`/
    `balance_paid` клиента в `True`. Побочный эффект флоу оплаты клиента —
    исключения ловит и логирует вызывающий код (`app.clients.service`), чтобы
    ошибка здесь не роняла основной запрос."""
    data = MoneyMovementCreate(subkind=MoneySubkind.SALE_INCOME, amount=amount, client_id=client_id)
    return create_money_movement(db, data, initiator_id=initiator_id)


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


def list_employee_salary_overview(db: Session) -> list[EmployeeSalaryOverview]:
    """0023: сотрудники (все активные пользователи — worker и admin) с их
    текущей незакрытой (draft/approved) зарплатной проводкой, если есть."""
    employees = db.query(User).filter(User.is_active.is_(True)).order_by(User.full_name).all()
    open_by_employee: dict[int, MoneyMovement] = {
        mm.employee_id: mm
        for mm in db.query(MoneyMovement)
        .filter(
            MoneyMovement.subkind == MoneySubkind.SALARY_PAYOUT,
            MoneyMovement.status.in_(_OPEN_STATUSES),
        )
        .all()
    }
    return [
        EmployeeSalaryOverview(
            employee_id=employee.id,
            full_name=employee.full_name,
            open_movement=MoneyMovementOut.from_movement(open_by_employee[employee.id])
            if employee.id in open_by_employee
            else None,
        )
        for employee in employees
    ]


def _adjust_supplier_paid(db: Session, mm: MoneyMovement, delta: float) -> None:
    """0011-f: `Supplier.total_paid` растёт при проведении оплаты поставки и
    уменьшается обратно, если проведённую оплату отменили — `balance`
    (`total_ordered - total_paid`) в ответе API остаётся консистентным."""
    if mm.subkind is not MoneySubkind.SUPPLY_PAYMENT or mm.supply_id is None:
        return
    order = db.get(SupplierOrder, mm.supply_id)
    if order is None or order.supplier is None:
        return
    order.supplier.total_paid = float(order.supplier.total_paid or 0) + delta


def change_status(
    db: Session,
    mm: MoneyMovement,
    to: MoneyMovementStatus,
    reason: str | None = None,
) -> MoneyMovement:
    if to not in _ALLOWED_TRANSITIONS[mm.status]:
        raise _conflict(f"Недопустимый переход статуса «{mm.status.value}» → «{to.value}»")

    was_draft = mm.status is MoneyMovementStatus.DRAFT
    was_posted = mm.status is MoneyMovementStatus.POSTED

    if to is MoneyMovementStatus.POSTED:
        if mm.amount is None or mm.amount <= 0:
            raise _conflict("Нельзя провести проводку без положительной суммы")
        if mm.subkind is None:
            raise _conflict("Нельзя провести проводку без вида")
        if mm.initiator_id is None:
            raise _conflict("Нельзя провести проводку без инициатора")
        mm.posted_at = _utcnow()
        _adjust_supplier_paid(db, mm, mm.amount)

    if to is MoneyMovementStatus.CANCELLED:
        cleaned = (reason or "").strip()
        if not cleaned:
            raise _bad_request("Отмена проводки требует указания причины")
        mm.cancel_reason = cleaned
        if was_posted:
            _adjust_supplier_paid(db, mm, -mm.amount)

    mm.status = to
    db.commit()
    db.refresh(mm)

    # Задача на согласование (0011-f) закрывается, как только проводка
    # покидает «черновик» — независимо от того, куда именно она перешла.
    if was_draft:
        task_service.close_open_link_task(db, TaskLinkType.MONEY_MOVEMENT_APPROVAL, mm.id)
        db.commit()

    return mm


# --------------------------------------------------------------------------- #
# Импорт платежей таблицей (задача 0011-k)                                    #
# --------------------------------------------------------------------------- #

_PRELIMINARY = {
    MoneyDirection.INCOME: MoneySubkind.OTHER_INCOME,
    MoneyDirection.EXPENSE: MoneySubkind.OTHER_EXPENSE,
}


@dataclass
class ImportOutcome:
    imported: int = 0
    skipped: int = 0
    created_ids: list[int] = field(default_factory=list)
    preliminary_subkind: int = 0  # проводок с «предварительным» видом
    unmatched_source: int = 0  # строк с контрагентом, не сопоставленным клиенту
    missing_payment_purpose: int = 0


def _norm_name(value: str) -> str:
    value = value.lower().replace("ё", "е")
    value = re.sub(r'["«»\'`]', "", value)
    value = re.sub(r"\b(ооо|оао|зао|пао|ип|ао)\b", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _match_client_by_name(db: Session, name: str | None) -> Client | None:
    """Уверенное совпадение по имени: точное (без ОПФ/регистра/кавычек) или
    единственное вхождение. Иначе None — гадать не будем."""
    if not name or len(name.strip()) < 3:
        return None
    target = _norm_name(name)
    if not target:
        return None
    clients = db.execute(select(Client)).scalars().all()
    exact = [c for c in clients if _norm_name(c.full_name) == target]
    if len(exact) == 1:
        return exact[0]
    if exact:
        return None
    contained = [
        c for c in clients
        if _norm_name(c.full_name) and (_norm_name(c.full_name) in target or target in _norm_name(c.full_name))
    ]
    return contained[0] if len(contained) == 1 else None


def import_payments(
    db: Session,
    headers: list[str],
    data: list[list[str]],
    mapping: payment_import.PaymentColumnMapping,
    initiator_id: int,
) -> ImportOutcome:
    built = payment_import.build_rows(headers, data, mapping)
    outcome = ImportOutcome(skipped=built.skipped)

    for row in built.items:
        subkind = row.subkind or _PRELIMINARY[row.direction]
        preliminary = row.subkind is None

        client = _match_client_by_name(db, row.counterparty)
        source_kind = MoneySourceKind.NONE
        client_id: int | None = None
        comment = None
        if client is not None:
            client_id = client.id
            source_kind = MoneySourceKind.CLIENT
            if preliminary and row.direction is MoneyDirection.INCOME:
                subkind = MoneySubkind.SALE_INCOME
                preliminary = False
        elif row.counterparty:
            comment = f"Контрагент: {row.counterparty.strip()}"
            outcome.unmatched_source += 1

        mm = MoneyMovement(
            direction=_direction_for(subkind),
            subkind=subkind,
            amount=row.amount,
            currency="RUB",
            tax=row.tax or 0,
            assessment=MoneyAssessment.ACTUAL,
            affects_profit=True,
            initiator_id=initiator_id,
            status=MoneyMovementStatus.DRAFT,
            doc_date=row.doc_date,
            payment_purpose=row.payment_purpose,
            comment=comment,
            external_number=row.external_number,
            source_kind=source_kind,
            client_id=client_id,
        )
        db.add(mm)
        db.flush()
        outcome.created_ids.append(mm.id)
        outcome.imported += 1
        if preliminary:
            outcome.preliminary_subkind += 1
        if not row.payment_purpose:
            outcome.missing_payment_purpose += 1

    db.commit()
    return outcome


_SUBKIND_TOOL = {
    "name": "assign_subkinds",
    "description": "Определить вид (подвид) проводки по назначению платежа и контрагенту.",
    "input_schema": {
        "type": "object",
        "properties": {
            "assignments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "subkind": {
                            "type": "string",
                            "description": "Строго одно из: sale_income, salary_payout, supply_payment, tax, rent, other_income, other_expense. Пусто, если не определяется.",
                        },
                    },
                    "required": ["id", "subkind"],
                },
            }
        },
        "required": ["assignments"],
    },
}


def ai_fill_subkinds(db: Session, movement_ids: list[int]) -> tuple[int, int]:
    """ИИ проставляет точный `subkind` черновым проводкам по назначению платежа
    и контрагенту. Меняет только draft; непроведённые/чужие статусы не трогает.
    Без ключа ИИ — (0, сколько просили)."""
    rows = (
        db.execute(
            select(MoneyMovement).where(
                MoneyMovement.id.in_(movement_ids or [-1]),
                MoneyMovement.status == MoneyMovementStatus.DRAFT,
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return 0, 0
    if not payment_import.ai_enabled():
        return 0, len(rows)

    from app.core.llm import anthropic_client

    listing = "\n".join(
        f"{r.id}. назначение: {r.payment_purpose or '—'} | контрагент: "
        f"{(r.comment or '').replace('Контрагент: ', '') or (r.client.full_name if r.client else '—')} | "
        f"направление: {r.direction.value}"
        for r in rows
    )
    user = (
        "Виды проводок: sale_income (доход от продажи), salary_payout (выплата "
        "зарплаты), supply_payment (оплата поставки), tax (налоги и сборы), rent "
        "(аренда), other_income (прочий доход), other_expense (прочий расход).\n\n"
        "Проводки (id. назначение | контрагент | направление):\n" + listing + "\n\n"
        "Для каждой выбери вид. Если не определяется однозначно — пустая строка."
    )
    try:
        client = anthropic_client(timeout=45.0, max_retries=2)
        response = client.messages.create(
            model=settings.ai_model,
            max_tokens=1024,
            system="Ты классифицируешь платежи по видам. Отвечай только вызовом assign_subkinds.",
            messages=[{"role": "user", "content": user}],
            tools=[_SUBKIND_TOOL],
            tool_choice={"type": "tool", "name": "assign_subkinds"},
        )
    except Exception:  # noqa: BLE001
        return 0, len(rows)

    block = next((b for b in response.content if b.type == "tool_use"), None)
    if block is None:
        return 0, len(rows)

    by_id = {r.id: r for r in rows}
    allowed = {m.value for m in MoneySubkind}
    updated = 0
    for item in (block.input or {}).get("assignments") or []:
        try:
            rid = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        val = str(item.get("subkind") or "").strip()
        mm = by_id.get(rid)
        if mm is None or val not in allowed:
            continue
        new_subkind = MoneySubkind(val)
        # не навешиваем вид, требующий источник, которого у проводки нет
        required = SUBKIND_REQUIRED_SOURCE.get(new_subkind)
        if required is not None and mm.source_kind is not required:
            continue
        if mm.subkind != new_subkind:
            mm.subkind = new_subkind
            mm.direction = _direction_for(new_subkind)
            updated += 1

    db.commit()
    return updated, len(rows) - updated


_BACKFILL_LABEL = {
    "subkind": "вид проводки",
    "payment_purpose": "назначение платежа",
    "source": "источник (контрагент)",
}


def create_import_backfill_task(
    db: Session, movement_ids: list[int], missing_fields: list[str]
) -> int:
    """Одна задача «дозаполнить проводки после импорта» — по кнопке в отчёте."""
    parts = [_BACKFILL_LABEL.get(m, m) for m in missing_fields] or ["проверить импортированные проводки"]
    count = len(movement_ids)
    assignees = user_service.users_with_access(db, AccessModule.ACCOUNTING)
    task = task_service.create_link_task(
        db,
        title=f"Дозаполнить {count} проводок после импорта: {', '.join(parts)}",
        link_type=TaskLinkType.MONEY_MOVEMENT_BACKFILL,
        link_id=movement_ids[0] if movement_ids else 0,
        assignees=assignees,
        link_meta={"movement_ids": movement_ids, "missing": missing_fields},
    )
    db.commit()
    return task.id


# --- Заказы у поставщика (задача 0011-d) ---

_ORDER_ALLOWED_TRANSITIONS: dict[SupplierOrderStatus, set[SupplierOrderStatus]] = {
    SupplierOrderStatus.ORDERED: {SupplierOrderStatus.IN_TRANSIT},
    SupplierOrderStatus.IN_TRANSIT: {SupplierOrderStatus.RECEIVED},
    SupplierOrderStatus.RECEIVED: set(),
}
_ORDER_EDITABLE_STATUSES = {SupplierOrderStatus.ORDERED, SupplierOrderStatus.IN_TRANSIT}


def _compute_order_total(items: list) -> float:
    total = 0.0
    for item in items:
        quantity = item.quantity if hasattr(item, "quantity") else item["quantity"]
        unit_price = item.unit_price if hasattr(item, "unit_price") else item["unit_price"]
        total += float(quantity) * float(unit_price)
    return total


def _validate_order_items(items: list) -> None:
    if not items:
        raise _bad_request("Заказ должен содержать хотя бы одну позицию")
    for item in items:
        material = item.material if hasattr(item, "material") else item.get("material")
        quantity = item.quantity if hasattr(item, "quantity") else item.get("quantity")
        unit_price = item.unit_price if hasattr(item, "unit_price") else item.get("unit_price")
        if not material:
            raise _bad_request("У позиции заказа не указан материал")
        if quantity is None or quantity <= 0:
            raise _bad_request(f"Позиция «{material}» — количество должно быть положительным")
        if unit_price is None or unit_price <= 0:
            raise _bad_request(f"Позиция «{material}» — цена должна быть положительной")


def get_supplier_order_or_404(db: Session, order_id: int) -> SupplierOrder:
    order = db.get(SupplierOrder, order_id)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Заказ не найден")
    return order


def list_supplier_orders(
    db: Session,
    *,
    supplier_id: int | None = None,
    status_: SupplierOrderStatus | None = None,
) -> list[SupplierOrder]:
    stmt = select(SupplierOrder)
    if supplier_id is not None:
        stmt = stmt.where(SupplierOrder.supplier_id == supplier_id)
    if status_ is not None:
        stmt = stmt.where(SupplierOrder.status == status_)
    stmt = stmt.order_by(SupplierOrder.created_at.desc(), SupplierOrder.id.desc())
    return list(db.execute(stmt).scalars().all())


def create_supplier_order(db: Session, data: SupplierOrderCreate) -> SupplierOrder:
    if db.get(Supplier, data.supplier_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Поставщик не найден")
    _validate_order_items(data.items)

    order = SupplierOrder(
        supplier_id=data.supplier_id,
        items=[item.model_dump() for item in data.items],
        total_cost=_compute_order_total(data.items),
        expected_at=data.expected_at,
        comment=data.comment,
        status=SupplierOrderStatus.ORDERED,
    )
    db.add(order)
    db.commit()
    warehouse_service.recalculate_supplier_totals(db, data.supplier_id)
    db.commit()
    db.refresh(order)
    return order


def update_supplier_order(db: Session, order: SupplierOrder, data: SupplierOrderUpdate) -> SupplierOrder:
    if order.status not in _ORDER_EDITABLE_STATUSES:
        raise _conflict(f"Заказ в статусе «{order.status.value}» редактировать нельзя")

    payload = data.model_dump(exclude_unset=True)
    if "items" in payload:
        _validate_order_items(data.items)
        order.items = [item.model_dump() for item in data.items]
        order.total_cost = _compute_order_total(data.items)
    if "expected_at" in payload:
        order.expected_at = data.expected_at
    if "comment" in payload:
        order.comment = data.comment

    db.commit()
    warehouse_service.recalculate_supplier_totals(db, order.supplier_id)
    db.commit()
    db.refresh(order)
    return order


def delete_supplier_order(db: Session, order: SupplierOrder) -> None:
    if order.status is not SupplierOrderStatus.ORDERED:
        raise _conflict("Удалить можно только заказ в статусе «заказана»")
    supplier_id = order.supplier_id
    db.delete(order)
    db.commit()
    warehouse_service.recalculate_supplier_totals(db, supplier_id)
    db.commit()


def change_supplier_order_status(
    db: Session, order: SupplierOrder, to: SupplierOrderStatus
) -> SupplierOrder:
    if to not in _ORDER_ALLOWED_TRANSITIONS[order.status]:
        raise _conflict(f"Недопустимый переход статуса «{order.status.value}» → «{to.value}»")
    order.status = to
    if to is SupplierOrderStatus.RECEIVED:
        order.received_at = _utcnow()
    db.commit()
    db.refresh(order)
    return order


def pay_supplier_order(db: Session, order: SupplierOrder, initiator_id: int) -> MoneyMovement:
    """0011-f: `POST /supplier-orders/{id}/pay` — создаёт проводку
    `supply_payment` на полную сумму заказа (MVP, частичная оплата не
    делается). Оплата не меняет статус физической приёмки заказа и наоборот —
    независимые состояния (см. родителя 0011, п. 6)."""
    data = MoneyMovementCreate(
        subkind=MoneySubkind.SUPPLY_PAYMENT, amount=order.total_cost, supply_id=order.id
    )
    return create_money_movement(db, data, initiator_id=initiator_id)
