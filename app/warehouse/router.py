from fastapi import Depends, FastAPI, HTTPException, UploadFile, status
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.common.module_access import Module as AccessModule
from app.core.deps import require_admin, require_module
from app.db.session import get_db
from app.production.schemas import MaterialRequestOut
from app.users.models import User
from app.warehouse import excel, price_import, service as warehouse_service
from app.warehouse.models import MaterialCategory, StockMovementReason, Supply, Warehouse
from app.warehouse.schemas import (
    AiFillCategoryResult,
    BackfillTaskRequest,
    BackfillTaskResult,
    LeadTimeQuestionDraft,
    LeadTimeQuestionSend,
    LeadTimeQuestionSent,
    LinkMaxChatIn,
    PriceListImportResult,
    StockMovementOut,
    SupplierCreate,
    SupplierNoteCreate,
    SupplierOut,
    SupplierPriceItemCreate,
    SupplierPriceItemUpdate,
    SupplierUpdate,
    SupplyCreate,
    SupplyOut,
    WarehouseMaterialCreate,
    WarehouseMaterialOut,
    WarehouseMaterialUpdate,
)

app = FastAPI(
    title="Soborbum — Склад",
    description="Материалы, поставки, заявки от производства и история движения материалов.",
    version="0.1.2",
)

require_warehouse = require_module(AccessModule.WAREHOUSE)


@app.get("/warehouses", response_model=list[str])
def list_warehouses(_: User = Depends(require_warehouse)):
    return [w.value for w in Warehouse]


@app.get("/categories", response_model=list[str])
def list_categories(_: User = Depends(require_warehouse)):
    return [c.value for c in MaterialCategory]


@app.get("/materials", response_model=list[WarehouseMaterialOut])
def list_materials(
    db: Session = Depends(get_db),
    _: User = Depends(require_warehouse),
    needs_supply: bool | None = None,
    warehouse: Warehouse | None = None,
):
    return warehouse_service.list_materials(db, only_needs_supply=bool(needs_supply), warehouse=warehouse)


@app.post("/materials", response_model=WarehouseMaterialOut, status_code=201)
def create_material(
    payload: WarehouseMaterialCreate, db: Session = Depends(get_db), _: User = Depends(require_warehouse)
):
    material = warehouse_service.create_material(db, payload)
    db.commit()
    return warehouse_service.to_out(db, material)


@app.get("/materials/{material_id}", response_model=WarehouseMaterialOut)
def get_material(material_id: int, db: Session = Depends(get_db), _: User = Depends(require_warehouse)):
    material = warehouse_service.get_material_or_404(db, material_id)
    return warehouse_service.to_out(db, material)


@app.patch("/materials/{material_id}", response_model=WarehouseMaterialOut)
def update_material(
    material_id: int,
    payload: WarehouseMaterialUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_warehouse),
):
    material = warehouse_service.get_material_or_404(db, material_id)
    material = warehouse_service.update_material(db, material, payload)
    db.commit()
    return warehouse_service.to_out(db, material)


@app.get("/materials/{material_id}/history", response_model=list[StockMovementOut])
def material_history(material_id: int, db: Session = Depends(get_db), _: User = Depends(require_warehouse)):
    warehouse_service.get_material_or_404(db, material_id)
    return warehouse_service.get_material_history(db, material_id)


@app.get("/history", response_model=list[StockMovementOut])
def history(
    db: Session = Depends(get_db),
    _: User = Depends(require_warehouse),
    material_id: int | None = None,
    reason: StockMovementReason | None = None,
):
    return warehouse_service.get_history(db, material_id=material_id, reason=reason)


@app.get("/supplies/template")
def download_supply_template(_: User = Depends(require_warehouse)):
    content = excel.generate_template()
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=supply_template.xlsx"},
    )


@app.post("/supplies", response_model=SupplyOut, status_code=201)
def create_supply(payload: SupplyCreate, db: Session = Depends(get_db), user: User = Depends(require_warehouse)):
    supply = warehouse_service.create_supply(db, payload, user)
    db.commit()
    db.refresh(supply)
    return supply


@app.post("/supplies/import", response_model=SupplyOut, status_code=201)
def import_supply(
    warehouse: Warehouse,
    file: UploadFile,
    db: Session = Depends(get_db),
    user: User = Depends(require_warehouse),
):
    rows = excel.parse_supply_rows(file)
    supply = warehouse_service.import_supply(db, rows, warehouse, user)
    db.commit()
    db.refresh(supply)
    return supply


@app.get("/supplies/{supply_id}", response_model=SupplyOut)
def get_supply(supply_id: int, db: Session = Depends(get_db), _: User = Depends(require_warehouse)):
    supply = db.get(Supply, supply_id)
    if not supply:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Поставка не найдена")
    return supply


@app.post("/requests/{request_id}/approve", response_model=MaterialRequestOut)
def approve_request(request_id: int, db: Session = Depends(get_db), user: User = Depends(require_warehouse)):
    request = warehouse_service.get_request_or_404(db, request_id)
    request = warehouse_service.approve_request(db, request, user)
    db.commit()
    db.refresh(request)
    return request


@app.post("/requests/{request_id}/reject", response_model=MaterialRequestOut)
def reject_request(request_id: int, db: Session = Depends(get_db), user: User = Depends(require_warehouse)):
    request = warehouse_service.get_request_or_404(db, request_id)
    request = warehouse_service.reject_request(db, request, user)
    db.commit()
    db.refresh(request)
    return request


# --- Поставщики (задача 0011-a) ---


@app.get("/suppliers", response_model=list[SupplierOut])
def list_suppliers(db: Session = Depends(get_db), _: User = Depends(require_warehouse)):
    return [warehouse_service.supplier_out(s) for s in warehouse_service.list_suppliers(db)]


@app.post("/suppliers", response_model=SupplierOut, status_code=201)
def create_supplier(payload: SupplierCreate, db: Session = Depends(get_db), _: User = Depends(require_warehouse)):
    supplier = warehouse_service.create_supplier(db, payload)
    db.commit()
    return warehouse_service.supplier_out(supplier)


@app.get("/suppliers/{supplier_id}", response_model=SupplierOut)
def get_supplier(supplier_id: int, db: Session = Depends(get_db), _: User = Depends(require_warehouse)):
    return warehouse_service.supplier_out(warehouse_service.get_supplier_or_404(db, supplier_id))


@app.patch("/suppliers/{supplier_id}", response_model=SupplierOut)
def update_supplier(
    supplier_id: int,
    payload: SupplierUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_warehouse),
):
    supplier = warehouse_service.get_supplier_or_404(db, supplier_id)
    supplier = warehouse_service.update_supplier(db, supplier, payload)
    db.commit()
    return warehouse_service.supplier_out(supplier)


@app.delete("/suppliers/{supplier_id}", status_code=204)
def delete_supplier(supplier_id: int, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    supplier = warehouse_service.get_supplier_or_404(db, supplier_id)
    warehouse_service.delete_supplier(db, supplier)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/suppliers/{supplier_id}/price-items/import", response_model=PriceListImportResult)
def import_price_list(
    supplier_id: int,
    file: UploadFile,
    db: Session = Depends(get_db),
    user: User = Depends(require_warehouse),
):
    """Импорт прайс-листа таблицей (.xlsx/.csv). Колонки размечает ИИ (при
    наличии `ANTHROPIC_API_KEY`), иначе — словарь синонимов. Строки добавляются
    к существующему прайсу; недостающие поля остаются пустыми и порождают задачу
    «дозаполнить». Без опознанных колонок материала и цены — отказ 400."""
    supplier = warehouse_service.get_supplier_or_404(db, supplier_id)
    headers, data = price_import.read_table(file)
    mapping = price_import.resolve_mapping(headers, data)

    if not mapping.material or (not mapping.price and not mapping.qty_breaks):
        which = "с названием материала" if not mapping.material else "с ценой"
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Не удалось определить колонку {which}. Заголовки файла: {headers}",
        )

    imported, skipped = warehouse_service.import_price_list(db, supplier, headers, data, mapping, user)
    db.commit()
    missing = mapping.missing_fields()
    return PriceListImportResult(
        supplier=warehouse_service.supplier_out(supplier),
        imported=imported,
        skipped=skipped,
        ai_used=mapping.ai_used,
        note=mapping.note,
        column_mapping=mapping.as_dict(),
        missing_fields=missing,
        backfill_suggested=bool(imported) and bool(missing or skipped),
    )


@app.post("/suppliers/{supplier_id}/price-items/ai-fill-category", response_model=AiFillCategoryResult)
def ai_fill_category(supplier_id: int, db: Session = Depends(get_db), _: User = Depends(require_warehouse)):
    """ИИ проставляет категорию из справочника склада строкам прайса поставщика,
    у которых она пуста."""
    supplier = warehouse_service.get_supplier_or_404(db, supplier_id)
    filled, skipped = warehouse_service.ai_fill_categories(db, supplier)
    db.commit()
    return AiFillCategoryResult(
        supplier=warehouse_service.supplier_out(supplier), filled=filled, skipped=skipped
    )


@app.post(
    "/suppliers/{supplier_id}/price-items/lead-time-question/draft",
    response_model=LeadTimeQuestionDraft,
)
def draft_lead_time_question(
    supplier_id: int, db: Session = Depends(get_db), _: User = Depends(require_warehouse)
):
    """Черновик сообщения поставщику в MAX с просьбой указать сроки поставки по
    позициям без срока. Ничего не отправляет. Нет привязанного чата MAX → 409."""
    supplier = warehouse_service.get_supplier_or_404(db, supplier_id)
    return LeadTimeQuestionDraft(**warehouse_service.draft_lead_time_question(supplier))


@app.post(
    "/suppliers/{supplier_id}/price-items/lead-time-question/send",
    response_model=LeadTimeQuestionSent,
)
def send_lead_time_question(
    supplier_id: int,
    payload: LeadTimeQuestionSend,
    db: Session = Depends(get_db),
    _: User = Depends(require_warehouse),
):
    """Отправляет согласованный пользователем текст в привязанный чат MAX поставщика."""
    supplier = warehouse_service.get_supplier_or_404(db, supplier_id)
    chat_id = warehouse_service.send_lead_time_question(supplier, payload.message)
    return LeadTimeQuestionSent(sent=True, chat_id=chat_id)


@app.post("/suppliers/{supplier_id}/price-items/backfill-task", response_model=BackfillTaskResult)
def create_backfill_task(
    supplier_id: int,
    payload: BackfillTaskRequest,
    db: Session = Depends(get_db),
    _: User = Depends(require_warehouse),
):
    """Создаёт задачу «дозаполнить прайс поставщика» по подтверждению пользователя."""
    supplier = warehouse_service.get_supplier_or_404(db, supplier_id)
    task_id = warehouse_service.create_backfill_task(db, supplier, payload.missing_fields)
    db.commit()
    return BackfillTaskResult(task_id=task_id, supplier=warehouse_service.supplier_out(supplier))


@app.post("/suppliers/{supplier_id}/price-items", response_model=SupplierOut, status_code=201)
def add_price_item(
    supplier_id: int,
    payload: SupplierPriceItemCreate,
    db: Session = Depends(get_db),
    _: User = Depends(require_warehouse),
):
    supplier = warehouse_service.get_supplier_or_404(db, supplier_id)
    warehouse_service.add_price_item(db, supplier, payload)
    db.commit()
    return warehouse_service.supplier_out(supplier)


@app.patch("/suppliers/{supplier_id}/price-items/{item_id}", response_model=SupplierOut)
def update_price_item(
    supplier_id: int,
    item_id: int,
    payload: SupplierPriceItemUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_warehouse),
):
    supplier = warehouse_service.get_supplier_or_404(db, supplier_id)
    warehouse_service.update_price_item(db, supplier, item_id, payload)
    db.commit()
    return warehouse_service.supplier_out(supplier)


@app.delete("/suppliers/{supplier_id}/price-items/{item_id}", status_code=204)
def delete_price_item(
    supplier_id: int,
    item_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_warehouse),
):
    supplier = warehouse_service.get_supplier_or_404(db, supplier_id)
    warehouse_service.delete_price_item(db, supplier, item_id)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/suppliers/{supplier_id}/notes", response_model=SupplierOut, status_code=201)
def add_supplier_note(
    supplier_id: int,
    payload: SupplierNoteCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_warehouse),
):
    supplier = warehouse_service.get_supplier_or_404(db, supplier_id)
    warehouse_service.add_supplier_note(db, supplier, user.id, payload.text)
    db.commit()
    return warehouse_service.supplier_out(supplier)


@app.delete("/suppliers/{supplier_id}/notes/{note_id}", response_model=SupplierOut)
def delete_supplier_note(
    supplier_id: int,
    note_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_warehouse),
):
    supplier = warehouse_service.get_supplier_or_404(db, supplier_id)
    warehouse_service.delete_supplier_note(db, supplier, note_id)
    db.commit()
    return warehouse_service.supplier_out(supplier)


@app.post("/suppliers/{supplier_id}/link-max-chat", response_model=SupplierOut)
def link_max_chat(
    supplier_id: int,
    payload: LinkMaxChatIn,
    db: Session = Depends(get_db),
    _: User = Depends(require_warehouse),
):
    supplier = warehouse_service.get_supplier_or_404(db, supplier_id)
    supplier = warehouse_service.link_max_chat(db, supplier, payload.chat_id)
    db.commit()
    return warehouse_service.supplier_out(supplier)


@app.delete("/suppliers/{supplier_id}/link-max-chat", response_model=SupplierOut)
def unlink_max_chat(supplier_id: int, db: Session = Depends(get_db), _: User = Depends(require_warehouse)):
    supplier = warehouse_service.get_supplier_or_404(db, supplier_id)
    supplier = warehouse_service.unlink_max_chat(db, supplier)
    db.commit()
    return warehouse_service.supplier_out(supplier)
