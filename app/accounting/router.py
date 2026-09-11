from datetime import datetime

from fastapi import Depends, FastAPI, HTTPException, Query, UploadFile, status
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.accounting import payment_import, service as accounting_service
from app.accounting.models import (
    MoneyDirection,
    MoneyMovementStatus,
    MoneySourceKind,
    MoneySubkind,
    SupplierOrderStatus,
)
from app.accounting.schemas import (
    AiFillSubkindRequest,
    AiFillSubkindResult,
    EmployeeSalaryOverview,
    ImportBackfillRequest,
    ImportBackfillResult,
    MoneyMovementCreate,
    MoneyMovementImportResult,
    MoneyMovementOut,
    MoneyMovementStatusChange,
    MoneyMovementUpdate,
    SupplierOrderCreate,
    SupplierOrderOut,
    SupplierOrderStatusChange,
    SupplierOrderUpdate,
)
from app.common.module_access import Module as AccessModule
from app.core.deps import require_admin, require_module
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


@app.get("/money-movements/import/template")
def download_import_template(_: User = Depends(require_accounting)):
    """.xlsx-шаблон таблицы платежей для импорта."""
    return Response(
        content=payment_import.generate_template(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=payments_template.xlsx"},
    )


@app.post("/money-movements/import", response_model=MoneyMovementImportResult)
def import_payments(
    file: UploadFile,
    db: Session = Depends(get_db),
    user: User = Depends(require_accounting),
):
    """Импорт платежей таблицей (.xlsx/.csv). Колонки размечает ИИ (при
    `ANTHROPIC_API_KEY`), иначе — словарь синонимов. Каждая строка → проводка в
    `draft`. Без критичных колонок (сумма / направление / дата / контрагент /
    НДС / номер документа) — отказ 400."""
    headers, data = payment_import.read_table(file)
    mapping = payment_import.resolve_mapping(headers, data)

    missing_critical = mapping.missing_critical()
    if missing_critical:
        which = ", ".join(payment_import.CRITICAL_LABELS.get(m, m) for m in missing_critical)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Не удалось определить колонку {which}. Заголовки файла: {headers}",
        )

    outcome = accounting_service.import_payments(db, headers, data, mapping, user.id)
    missing_optional = mapping.missing_optional()
    return MoneyMovementImportResult(
        imported=outcome.imported,
        skipped=outcome.skipped,
        ai_used=mapping.ai_used,
        note=mapping.note,
        column_mapping=mapping.as_dict(),
        missing_fields=missing_optional,
        unmatched_source=outcome.unmatched_source,
        preliminary_subkind=outcome.preliminary_subkind,
        created_ids=outcome.created_ids,
        backfill_suggested=bool(outcome.imported)
        and bool(outcome.preliminary_subkind or outcome.unmatched_source or missing_optional or outcome.skipped),
    )


@app.post("/money-movements/import/ai-fill-subkind", response_model=AiFillSubkindResult)
def ai_fill_subkind(
    payload: AiFillSubkindRequest,
    db: Session = Depends(get_db),
    _: User = Depends(require_accounting),
):
    """ИИ уточняет вид (`subkind`) у черновых проводок по назначению платежа и
    контрагенту. Меняет только `draft`. Без ключа ИИ — `updated = 0`."""
    updated, skipped = accounting_service.ai_fill_subkinds(db, payload.movement_ids)
    return AiFillSubkindResult(updated=updated, skipped=skipped)


@app.post("/money-movements/import/backfill-task", response_model=ImportBackfillResult)
def create_import_backfill_task(
    payload: ImportBackfillRequest,
    db: Session = Depends(get_db),
    _: User = Depends(require_accounting),
):
    """Одна задача «дозаполнить проводки после импорта» — по кнопке в отчёте."""
    task_id = accounting_service.create_import_backfill_task(
        db, payload.movement_ids, payload.missing_fields
    )
    return ImportBackfillResult(task_id=task_id)


@app.get("/salary-overview", response_model=list[EmployeeSalaryOverview])
def salary_overview(db: Session = Depends(get_db), _: User = Depends(require_accounting)):
    """0023: раздел «Сотрудники» — каждый активный сотрудник и его текущая
    незакрытая зарплатная проводка (если есть), для кнопок «Начислить» /
    «Утвердить» / «Выплатить»."""
    return accounting_service.list_employee_salary_overview(db)


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
    mm_id: int, db: Session = Depends(get_db), _: User = Depends(require_admin)
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


# --- Заказы у поставщика (задача 0011-d) ---


@app.get("/supplier-orders", response_model=list[SupplierOrderOut])
def list_supplier_orders(
    db: Session = Depends(get_db),
    _: User = Depends(require_accounting),
    supplier_id: int | None = None,
    status_filter: SupplierOrderStatus | None = Query(None, alias="status"),
):
    orders = accounting_service.list_supplier_orders(db, supplier_id=supplier_id, status_=status_filter)
    return [SupplierOrderOut.from_order(o) for o in orders]


@app.post("/supplier-orders", response_model=SupplierOrderOut, status_code=201)
def create_supplier_order(
    payload: SupplierOrderCreate,
    db: Session = Depends(get_db),
    _: User = Depends(require_accounting),
):
    order = accounting_service.create_supplier_order(db, payload)
    return SupplierOrderOut.from_order(order)


@app.get("/supplier-orders/{order_id}", response_model=SupplierOrderOut)
def get_supplier_order(
    order_id: int, db: Session = Depends(get_db), _: User = Depends(require_accounting)
):
    return SupplierOrderOut.from_order(accounting_service.get_supplier_order_or_404(db, order_id))


@app.patch("/supplier-orders/{order_id}", response_model=SupplierOrderOut)
def update_supplier_order(
    order_id: int,
    payload: SupplierOrderUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_accounting),
):
    order = accounting_service.get_supplier_order_or_404(db, order_id)
    order = accounting_service.update_supplier_order(db, order, payload)
    return SupplierOrderOut.from_order(order)


@app.delete("/supplier-orders/{order_id}", status_code=204)
def delete_supplier_order(
    order_id: int, db: Session = Depends(get_db), _: User = Depends(require_accounting)
):
    order = accounting_service.get_supplier_order_or_404(db, order_id)
    accounting_service.delete_supplier_order(db, order)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/supplier-orders/{order_id}/status", response_model=SupplierOrderOut)
def change_supplier_order_status(
    order_id: int,
    payload: SupplierOrderStatusChange,
    db: Session = Depends(get_db),
    _: User = Depends(require_accounting),
):
    order = accounting_service.get_supplier_order_or_404(db, order_id)
    order = accounting_service.change_supplier_order_status(db, order, payload.to)
    return SupplierOrderOut.from_order(order)
