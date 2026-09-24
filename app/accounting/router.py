from datetime import datetime

from fastapi import Depends, FastAPI, HTTPException, Query, UploadFile, status
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.accounting import payment_import, service as accounting_service
from app.accounting.models import (
    CounterpartyKind,
    MoneyDirection,
    MoneyMovementStatus,
    MoneySourceKind,
    MoneySubkind,
    SupplierOrderStatus,
)
from app.accounting.schemas import (
    AiFillSubkindRequest,
    AiFillSubkindResult,
    BankAccountCreate,
    BankAccountOut,
    BankAccountUpdate,
    CounterpartyCreate,
    CounterpartyOut,
    CounterpartyUpdate,
    EmployeeKpiPeriod,
    EmployeeSalaryOverview,
    ImportBackfillRequest,
    ImportBackfillResult,
    MoneyMovementCreate,
    MoneyMovementImportResult,
    MoneyMovementOut,
    MoneyMovementStatusChange,
    MoneyMovementUpdate,
    MoneySummaryOut,
    OrganizationCreate,
    OrganizationOut,
    OrganizationUpdate,
    SupplierOrderCreate,
    SupplierOrderOut,
    SupplierOrderStatusChange,
    SupplierOrderUpdate,
)
from app.common.files import FileAssetOut, FilePurpose, save_upload_file
from app.common.module_access import Module as AccessModule
from app.core.deps import require_edit, require_full, require_view
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

require_accounting_view = require_view(AccessModule.ACCOUNTING)
require_accounting_edit = require_edit(AccessModule.ACCOUNTING)
require_accounting_full = require_full(AccessModule.ACCOUNTING)


@app.get("/money-movements/enums")
def money_movement_enums(_: User = Depends(require_accounting_view)):
    """Справочники для фронта: виды, статусы, оценки, типы источников."""
    return {
        "direction": [e.value for e in MoneyDirection],
        "subkind": [e.value for e in MoneySubkind],
        "status": [e.value for e in MoneyMovementStatus],
        "source_kind": [e.value for e in MoneySourceKind],
    }


@app.get("/money-movements/import/template")
def download_import_template(_: User = Depends(require_accounting_view)):
    """.xlsx-шаблон выписки для импорта."""
    return Response(
        content=payment_import.generate_template(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=payments_template.xlsx"},
    )


@app.post("/money-movements/import", response_model=MoneyMovementImportResult)
def import_payments(
    file: UploadFile,
    account_id: int = Query(..., description="Счёт, на который легли платежи выписки"),
    db: Session = Depends(get_db),
    user: User = Depends(require_accounting_edit),
):
    """Импорт выписки из банк-клиента (.xlsx/.csv) на конкретный счёт.
    Колонки размечает ИИ (при `ANTHROPIC_API_KEY`), иначе — словарь синонимов.
    Каждая строка → проводка в `draft` на `account_id`, её контрагент
    сопоставляется с единым справочником (0081-e). Без критичных колонок
    (сумма / направление / дата / контрагент / НДС / номер документа) —
    отказ 400; без счёта — 422."""
    account = accounting_service.get_account_or_404(db, account_id)
    headers, data = payment_import.read_table(file)
    mapping = payment_import.resolve_mapping(headers, data)

    missing_critical = mapping.missing_critical()
    if missing_critical:
        which = ", ".join(payment_import.CRITICAL_LABELS.get(m, m) for m in missing_critical)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Не удалось определить колонку {which}. Заголовки файла: {headers}",
        )

    outcome = accounting_service.import_payments(
        db, headers, data, mapping, user.id, account_id=account.id
    )
    missing_optional = mapping.missing_optional()
    return MoneyMovementImportResult(
        imported=outcome.imported,
        skipped=outcome.skipped,
        ai_used=mapping.ai_used,
        note=mapping.note,
        column_mapping=mapping.as_dict(),
        missing_fields=missing_optional,
        unmatched_source=outcome.unmatched_source,
        duplicates=outcome.duplicates,
        counterparties_created=outcome.counterparties_created,
        counterparties_matched=outcome.counterparties_matched,
        account_id=account.id,
        account_label=f"{account.organization.short_name} — {account.name}"
        if account.organization
        else account.name,
        preliminary_subkind=outcome.preliminary_subkind,
        created_ids=outcome.created_ids,
        backfill_suggested=bool(outcome.imported)
        and bool(outcome.preliminary_subkind or outcome.unmatched_source or missing_optional or outcome.skipped),
    )


@app.post("/money-movements/import/ai-fill-subkind", response_model=AiFillSubkindResult)
def ai_fill_subkind(
    payload: AiFillSubkindRequest,
    db: Session = Depends(get_db),
    _: User = Depends(require_accounting_edit),
):
    """ИИ уточняет вид (`subkind`) у черновых проводок по назначению платежа и
    контрагенту. Меняет только `draft`. Без ключа ИИ — `updated = 0`."""
    updated, skipped = accounting_service.ai_fill_subkinds(db, payload.movement_ids)
    return AiFillSubkindResult(updated=updated, skipped=skipped)


@app.post("/money-movements/import/backfill-task", response_model=ImportBackfillResult)
def create_import_backfill_task(
    payload: ImportBackfillRequest,
    db: Session = Depends(get_db),
    _: User = Depends(require_accounting_edit),
):
    """Одна задача «дозаполнить проводки после импорта» — по кнопке в отчёте."""
    task_id = accounting_service.create_import_backfill_task(
        db, payload.movement_ids, payload.missing_fields
    )
    return ImportBackfillResult(task_id=task_id)


@app.get("/salary-overview", response_model=list[EmployeeSalaryOverview])
def salary_overview(db: Session = Depends(get_db), _: User = Depends(require_accounting_view)):
    """0023: раздел «Сотрудники» — каждый активный сотрудник и его текущая
    незакрытая зарплатная проводка (если есть), для кнопок «Начислить» /
    «Утвердить» / «Выплатить»."""
    return accounting_service.list_employee_salary_overview(db)


@app.get("/employee-kpi-history/{employee_id}", response_model=list[EmployeeKpiPeriod])
def employee_kpi_history(
    employee_id: int, db: Session = Depends(get_db), _: User = Depends(require_accounting_view)
):
    """0042: до 6 последних периодов KPI сотрудника (текущий месяц —
    пересчитан на момент запроса), новые сверху — для карточки в 0041."""
    return accounting_service.get_employee_kpi_history(db, employee_id)



# --- Организации и банковские счета (0081-a) ---


@app.get("/organizations", response_model=list[OrganizationOut])
def list_organizations(
    db: Session = Depends(get_db),
    _: User = Depends(require_accounting_view),
    include_inactive: bool = False,
):
    """Юрлица компании со вложенными счетами — вкладки раздела «Бухгалтерия»."""
    return accounting_service.list_organizations(db, include_inactive=include_inactive)


@app.post("/organizations", response_model=OrganizationOut, status_code=201)
def create_organization(
    payload: OrganizationCreate,
    db: Session = Depends(get_db),
    _: User = Depends(require_accounting_edit),
):
    return accounting_service.create_organization(db, payload)


@app.patch("/organizations/{org_id}", response_model=OrganizationOut)
def update_organization(
    org_id: int,
    payload: OrganizationUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_accounting_edit),
):
    org = accounting_service.get_organization_or_404(db, org_id)
    return accounting_service.update_organization(db, org, payload)


@app.get("/accounts", response_model=list[BankAccountOut])
def list_accounts(
    db: Session = Depends(get_db),
    _: User = Depends(require_accounting_view),
    organization_id: int | None = None,
    include_inactive: bool = False,
):
    return accounting_service.list_accounts(
        db, organization_id=organization_id, include_inactive=include_inactive
    )


@app.post("/accounts", response_model=BankAccountOut, status_code=201)
def create_account(
    payload: BankAccountCreate,
    db: Session = Depends(get_db),
    _: User = Depends(require_accounting_edit),
):
    return accounting_service.create_account(db, payload)


@app.patch("/accounts/{account_id}", response_model=BankAccountOut)
def update_account(
    account_id: int,
    payload: BankAccountUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_accounting_edit),
):
    """Счёт не удаляется — закрывается через `is_active = false`: у него
    остаются проводки, которые нельзя осиротить."""
    account = accounting_service.get_account_or_404(db, account_id)
    return accounting_service.update_account(db, account, payload)


@app.get("/money-summary", response_model=MoneySummaryOut)
def money_summary(
    db: Session = Depends(get_db),
    _: User = Depends(require_accounting_view),
    organization_id: int | None = None,
    account_id: int | None = None,
    status_filter: MoneyMovementStatus | None = Query(MoneyMovementStatus.POSTED, alias="status"),
    date_from: datetime | None = None,
    date_to: datetime | None = None,
):
    """Приход, расход и сальдо — по каждому счёту, организации и итогом.
    По умолчанию считаются только проведённые проводки."""
    return accounting_service.money_summary(
        db,
        organization_id=organization_id,
        account_id=account_id,
        status_=status_filter,
        date_from=date_from,
        date_to=date_to,
    )



# --- Единый справочник контрагентов (0081-c) ---


@app.get("/counterparties", response_model=list[CounterpartyOut])
def list_counterparties(
    db: Session = Depends(get_db),
    _: User = Depends(require_accounting_view),
    query: str | None = None,
    kind: CounterpartyKind | None = None,
    include_inactive: bool = False,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Справочник с поиском по наименованию и ИНН. У каждой строки — суммы
    прихода/расхода по её проведённым платежам."""
    found = accounting_service.list_counterparties(
        db,
        query=query,
        kind=kind,
        is_active=None if include_inactive else True,
        limit=limit,
        offset=offset,
    )
    return [accounting_service.counterparty_out(db, c) for c in found]


@app.post("/counterparties", response_model=CounterpartyOut, status_code=201)
def create_counterparty(
    payload: CounterpartyCreate,
    db: Session = Depends(get_db),
    _: User = Depends(require_accounting_edit),
):
    counterparty = accounting_service.create_counterparty(db, payload)
    return accounting_service.counterparty_out(db, counterparty)


@app.get("/counterparties/{counterparty_id}", response_model=CounterpartyOut)
def get_counterparty(
    counterparty_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_accounting_view),
):
    counterparty = accounting_service.get_counterparty_or_404(db, counterparty_id)
    return accounting_service.counterparty_out(db, counterparty)


@app.patch("/counterparties/{counterparty_id}", response_model=CounterpartyOut)
def update_counterparty(
    counterparty_id: int,
    payload: CounterpartyUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_accounting_edit),
):
    """Контрагент не удаляется — уходит в `is_active = false`: на нём висит
    история платежей, которую нельзя осиротить."""
    counterparty = accounting_service.get_counterparty_or_404(db, counterparty_id)
    counterparty = accounting_service.update_counterparty(db, counterparty, payload)
    return accounting_service.counterparty_out(db, counterparty)


@app.get("/counterparties/{counterparty_id}/payments", response_model=list[MoneyMovementOut])
def counterparty_payments(
    counterparty_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_accounting_view),
    account_id: int | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """История платежей контрагента по всем счетам, новые сверху."""
    accounting_service.get_counterparty_or_404(db, counterparty_id)
    payments = accounting_service.list_counterparty_payments(
        db,
        counterparty_id,
        account_id=account_id,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )
    return [MoneyMovementOut.from_movement(mm) for mm in payments]


@app.get("/money-movements", response_model=list[MoneyMovementOut])
def list_money_movements(
    db: Session = Depends(get_db),
    _: User = Depends(require_accounting_view),
    direction: MoneyDirection | None = None,
    subkind: MoneySubkind | None = None,
    status_filter: MoneyMovementStatus | None = Query(None, alias="status"),
    source_kind: MoneySourceKind | None = None,
    client_id: int | None = None,
    employee_id: int | None = None,
    supply_id: int | None = None,
    counterparty_id: int | None = None,
    initiator_id: int | None = None,
    account_id: int | None = None,
    organization_id: int | None = None,
    amount_min: float | None = None,
    amount_max: float | None = None,
    tax_min: float | None = None,
    tax_max: float | None = None,
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
        counterparty_id=counterparty_id,
        initiator_id=initiator_id,
        account_id=account_id,
        organization_id=organization_id,
        amount_min=amount_min,
        amount_max=amount_max,
        tax_min=tax_min,
        tax_max=tax_max,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )
    return [MoneyMovementOut.from_movement(mm) for mm in movements]


@app.post("/money-movement-documents", response_model=FileAssetOut)
def upload_money_movement_document(
    file: UploadFile,
    db: Session = Depends(get_db),
    user: User = Depends(require_accounting_edit),
):
    """Загрузить документ для прикрепления к проводке (0072-d): вернувшийся
    id передаётся в `document_ids` при создании/правке проводки. Разрешена
    загрузка «на будущее» — до того, как файл действительно приложен к
    какой-то проводке, тот же приём, что `image_ids` у задач."""
    asset = save_upload_file(db, file, FilePurpose.MONEY_MOVEMENT_DOCUMENT, user)
    db.commit()
    db.refresh(asset)
    return asset


@app.post("/money-movements", response_model=MoneyMovementOut, status_code=201)
def create_money_movement(
    payload: MoneyMovementCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_accounting_edit),
):
    """Счёт (`account_id`) обязателен: проводка, заведённая человеком, всегда
    относится к конкретному счёту организации (0081-a). Подстановка счёта по
    умолчанию оставлена только авто-проводкам внутри бэка (0011-f)."""
    if payload.account_id is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Выберите счёт, по которому прошёл платёж",
        )
    mm = accounting_service.create_money_movement(
        db, payload, initiator_id=payload.initiator_id or user.id
    )
    return MoneyMovementOut.from_movement(mm)


@app.get("/money-movements/{mm_id}", response_model=MoneyMovementOut)
def get_money_movement(
    mm_id: int, db: Session = Depends(get_db), _: User = Depends(require_accounting_view)
):
    return MoneyMovementOut.from_movement(accounting_service.get_money_movement(db, mm_id))


@app.patch("/money-movements/{mm_id}", response_model=MoneyMovementOut)
def update_money_movement(
    mm_id: int,
    payload: MoneyMovementUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_accounting_edit),
):
    mm = accounting_service.get_money_movement(db, mm_id)
    mm = accounting_service.update_money_movement(db, mm, payload)
    return MoneyMovementOut.from_movement(mm)


@app.delete("/money-movements/{mm_id}", status_code=204)
def delete_money_movement(
    mm_id: int, db: Session = Depends(get_db), _: User = Depends(require_accounting_full)
):
    mm = accounting_service.get_money_movement(db, mm_id)
    accounting_service.delete_money_movement(db, mm)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/money-movements/{mm_id}/status", response_model=MoneyMovementOut)
def change_money_movement_status(
    mm_id: int,
    payload: MoneyMovementStatusChange,
    db: Session = Depends(get_db),
    _: User = Depends(require_accounting_edit),
):
    mm = accounting_service.get_money_movement(db, mm_id)
    mm = accounting_service.change_status(db, mm, payload.to, payload.reason)
    return MoneyMovementOut.from_movement(mm)


# --- Заказы у поставщика (задача 0011-d) ---


@app.get("/supplier-orders", response_model=list[SupplierOrderOut])
def list_supplier_orders(
    db: Session = Depends(get_db),
    _: User = Depends(require_accounting_view),
    supplier_id: int | None = None,
    status_filter: SupplierOrderStatus | None = Query(None, alias="status"),
):
    orders = accounting_service.list_supplier_orders(db, supplier_id=supplier_id, status_=status_filter)
    return [SupplierOrderOut.from_order(o) for o in orders]


@app.post("/supplier-orders", response_model=SupplierOrderOut, status_code=201)
def create_supplier_order(
    payload: SupplierOrderCreate,
    db: Session = Depends(get_db),
    _: User = Depends(require_accounting_edit),
):
    order = accounting_service.create_supplier_order(db, payload)
    return SupplierOrderOut.from_order(order)


@app.get("/supplier-orders/{order_id}", response_model=SupplierOrderOut)
def get_supplier_order(
    order_id: int, db: Session = Depends(get_db), _: User = Depends(require_accounting_view)
):
    return SupplierOrderOut.from_order(accounting_service.get_supplier_order_or_404(db, order_id))


@app.patch("/supplier-orders/{order_id}", response_model=SupplierOrderOut)
def update_supplier_order(
    order_id: int,
    payload: SupplierOrderUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_accounting_edit),
):
    order = accounting_service.get_supplier_order_or_404(db, order_id)
    order = accounting_service.update_supplier_order(db, order, payload)
    return SupplierOrderOut.from_order(order)


@app.delete("/supplier-orders/{order_id}", status_code=204)
def delete_supplier_order(
    order_id: int, db: Session = Depends(get_db), _: User = Depends(require_accounting_edit)
):
    order = accounting_service.get_supplier_order_or_404(db, order_id)
    accounting_service.delete_supplier_order(db, order)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/supplier-orders/{order_id}/status", response_model=SupplierOrderOut)
def change_supplier_order_status(
    order_id: int,
    payload: SupplierOrderStatusChange,
    db: Session = Depends(get_db),
    _: User = Depends(require_accounting_edit),
):
    order = accounting_service.get_supplier_order_or_404(db, order_id)
    order = accounting_service.change_supplier_order_status(db, order, payload.to)
    return SupplierOrderOut.from_order(order)


@app.post("/supplier-orders/{order_id}/pay", response_model=MoneyMovementOut, status_code=201)
def pay_supplier_order(
    order_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_accounting_edit),
):
    """0011-f: создаёт проводку «оплата поставки» на полную сумму заказа.
    Повторный вызов, пока заказ уже оплачивается/оплачен, — `409`."""
    order = accounting_service.get_supplier_order_or_404(db, order_id)
    mm = accounting_service.pay_supplier_order(db, order, initiator_id=user.id)
    return MoneyMovementOut.from_movement(mm)
