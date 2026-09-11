from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.accounting.models import SupplierOrder
from app.common.module_access import Module as AccessModule
from app.production.models import MaterialRequest, MaterialRequestStatus, ModuleMaterial, ProductionModule
from app.tasks import service as task_service
from app.tasks.models import Task, TaskLinkType, TaskStatus
from app.users import service as user_service
from app.users.models import User
from app.warehouse import price_import
from app.warehouse.price_import import ColumnMapping
from app.warehouse.models import (
    StockMovement,
    StockMovementReason,
    Supplier,
    SupplierNote,
    SupplierPriceItem,
    Supply,
    SupplyLine,
    Warehouse,
    WarehouseMaterial,
)
from app.warehouse.schemas import (
    RequestBreakdownItem,
    SupplierCreate,
    SupplierNoteOut,
    SupplierOut,
    SupplierPriceItemCreate,
    SupplierPriceItemOut,
    SupplierPriceItemUpdate,
    SupplierUpdate,
    SupplyCreate,
    WarehouseMaterialCreate,
    WarehouseMaterialOut,
    WarehouseMaterialUpdate,
)


def get_material_or_404(db: Session, material_id: int) -> WarehouseMaterial:
    material = db.get(WarehouseMaterial, material_id)
    if not material:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Материал не найден")
    return material


def get_request_or_404(db: Session, request_id: int) -> MaterialRequest:
    request = db.get(MaterialRequest, request_id)
    if not request:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Заявка на материал не найдена")
    return request


def create_material(db: Session, payload: WarehouseMaterialCreate) -> WarehouseMaterial:
    material = WarehouseMaterial(**payload.model_dump())
    db.add(material)
    db.flush()
    sync_shortage_task(db, material)
    return material


def update_material(db: Session, material: WarehouseMaterial, payload: WarehouseMaterialUpdate) -> WarehouseMaterial:
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(material, field, value)
    db.flush()
    sync_shortage_task(db, material)
    return material


def compute_breakdown(db: Session, material_id: int) -> list[RequestBreakdownItem]:
    rows = (
        db.query(ModuleMaterial.module_id, ProductionModule.name, ProductionModule.production_id, MaterialRequest.quantity)
        .join(MaterialRequest, MaterialRequest.module_material_id == ModuleMaterial.id)
        .join(ProductionModule, ProductionModule.id == ModuleMaterial.module_id)
        .filter(
            MaterialRequest.warehouse_material_id == material_id,
            MaterialRequest.status == MaterialRequestStatus.PENDING,
        )
        .all()
    )
    breakdown: dict[int, RequestBreakdownItem] = {}
    for module_id, module_name, production_id, quantity in rows:
        if module_id in breakdown:
            breakdown[module_id].quantity_requested += float(quantity)
        else:
            breakdown[module_id] = RequestBreakdownItem(
                module_id=module_id,
                module_name=module_name,
                production_id=production_id,
                quantity_requested=float(quantity),
            )
    return list(breakdown.values())


def total_requested(db: Session, material_id: int) -> float:
    return sum(item.quantity_requested for item in compute_breakdown(db, material_id))


def needs_supply(material: WarehouseMaterial, requested: float) -> bool:
    return (float(material.quantity_in_stock) - requested) < float(material.threshold)


def to_out(db: Session, material: WarehouseMaterial) -> WarehouseMaterialOut:
    breakdown = compute_breakdown(db, material.id)
    requested = sum(item.quantity_requested for item in breakdown)
    return WarehouseMaterialOut(
        id=material.id,
        warehouse=material.warehouse,
        category=material.category,
        title=material.title,
        code=material.code,
        unit=material.unit,
        is_fractional=material.is_fractional,
        quantity_in_stock=float(material.quantity_in_stock),
        purchase_price=float(material.purchase_price),
        threshold=float(material.threshold),
        total_requested=requested,
        needs_supply=needs_supply(material, requested),
        request_breakdown=breakdown,
        created_at=material.created_at,
    )


def list_materials(
    db: Session, only_needs_supply: bool = False, warehouse: Warehouse | None = None
) -> list[WarehouseMaterialOut]:
    query = db.query(WarehouseMaterial)
    if warehouse is not None:
        query = query.filter(WarehouseMaterial.warehouse == warehouse)
    materials = query.order_by(WarehouseMaterial.id).all()
    result = [to_out(db, m) for m in materials]
    if only_needs_supply:
        result = [r for r in result if r.needs_supply]
    return result


SHORTAGE_TITLE = "Заказать дефицитный материал: {title}"


def sync_shortage_task(db: Session, material: WarehouseMaterial) -> None:
    requested = total_requested(db, material.id)
    open_task = (
        db.query(Task)
        .filter(
            Task.link_type == TaskLinkType.WAREHOUSE_SHORTAGE,
            Task.link_id == material.id,
            Task.status != TaskStatus.DONE,
        )
        .first()
    )
    if needs_supply(material, requested):
        if not open_task:
            assignees = user_service.users_with_access(db, AccessModule.WAREHOUSE)
            task_service.create_link_task(
                db,
                title=SHORTAGE_TITLE.format(title=material.title),
                link_type=TaskLinkType.WAREHOUSE_SHORTAGE,
                link_id=material.id,
                assignees=assignees,
            )
    else:
        if open_task:
            task_service.force_close(db, open_task)


def log_movement(
    db: Session,
    material: WarehouseMaterial,
    delta: float,
    reason: StockMovementReason,
    created_by: User,
    reference_id: int | None = None,
) -> StockMovement:
    movement = StockMovement(
        warehouse_material_id=material.id,
        delta=delta,
        reason=reason,
        reference_id=reference_id,
        created_by_id=created_by.id,
    )
    db.add(movement)
    db.flush()
    return movement


def approve_request(db: Session, request: MaterialRequest, decided_by: User) -> MaterialRequest:
    if request.status != MaterialRequestStatus.PENDING:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Заявка уже обработана")

    module_material = request.module_material
    warehouse_material = request.warehouse_material

    module_material.quantity_requested -= request.quantity
    module_material.quantity_provided += request.quantity
    warehouse_material.quantity_in_stock -= request.quantity

    request.status = MaterialRequestStatus.APPROVED
    request.decided_by_id = decided_by.id
    request.decided_at = datetime.now(timezone.utc)
    db.flush()

    log_movement(db, warehouse_material, -float(request.quantity), StockMovementReason.ISSUED, decided_by, request.id)

    if request.task_id:
        task = db.get(Task, request.task_id)
        if task:
            task_service.force_close(db, task)

    sync_shortage_task(db, warehouse_material)
    return request


def reject_request(db: Session, request: MaterialRequest, decided_by: User) -> MaterialRequest:
    if request.status != MaterialRequestStatus.PENDING:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Заявка уже обработана")

    module_material = request.module_material
    warehouse_material = request.warehouse_material

    module_material.quantity_requested -= request.quantity
    module_material.quantity_required += request.quantity

    request.status = MaterialRequestStatus.REJECTED
    request.decided_by_id = decided_by.id
    request.decided_at = datetime.now(timezone.utc)
    db.flush()

    log_movement(db, warehouse_material, 0, StockMovementReason.REQUEST_REJECTED_RETURN, decided_by, request.id)

    if request.task_id:
        task = db.get(Task, request.task_id)
        if task:
            task_service.force_close(db, task)

    sync_shortage_task(db, warehouse_material)
    return request


def create_supply(db: Session, payload: SupplyCreate, created_by: User) -> Supply:
    materials = {line.warehouse_material_id: get_material_or_404(db, line.warehouse_material_id) for line in payload.lines}
    warehouses = {m.warehouse for m in materials.values()}
    if len(warehouses) > 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Поставка должна ссылаться на материалы только одного склада",
        )

    supply = Supply(supplier_name=payload.supplier_name, created_by_id=created_by.id)
    db.add(supply)
    db.flush()

    for line in payload.lines:
        material = materials[line.warehouse_material_id]
        db.add(SupplyLine(supply_id=supply.id, warehouse_material_id=material.id, quantity=line.quantity))
        material.quantity_in_stock += line.quantity
        db.flush()
        log_movement(db, material, float(line.quantity), StockMovementReason.SUPPLY, created_by, supply.id)
        sync_shortage_task(db, material)

    db.flush()
    return supply


def import_supply(db: Session, rows: list[dict], warehouse: Warehouse, created_by: User) -> Supply:
    supply = Supply(supplier_name=None, created_by_id=created_by.id)
    db.add(supply)
    db.flush()

    for row in rows:
        material: WarehouseMaterial | None = None
        if row["warehouse_material_id"] is not None:
            material = get_material_or_404(db, row["warehouse_material_id"])
            if material.warehouse != warehouse:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Материал «{material.title}» принадлежит другому складу",
                )
        else:
            material = (
                db.query(WarehouseMaterial)
                .filter_by(warehouse=warehouse, code=row["code"])
                .first()
            )
            if material is None:
                if not row["unit"]:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Для нового материала «{row['title']}» нужно указать единицу измерения",
                    )
                material = WarehouseMaterial(
                    warehouse=warehouse,
                    category=row["category"],
                    code=row["code"],
                    title=row["title"],
                    unit=row["unit"],
                    is_fractional=row["is_fractional"],
                    purchase_price=row["purchase_price"],
                    quantity_in_stock=0,
                    threshold=0,
                )
                db.add(material)
                db.flush()

        db.add(SupplyLine(supply_id=supply.id, warehouse_material_id=material.id, quantity=row["quantity"]))
        material.quantity_in_stock += row["quantity"]
        db.flush()
        log_movement(db, material, float(row["quantity"]), StockMovementReason.SUPPLY, created_by, supply.id)
        sync_shortage_task(db, material)

    db.flush()
    return supply


def get_material_history(db: Session, material_id: int) -> list[StockMovement]:
    return (
        db.query(StockMovement)
        .filter(StockMovement.warehouse_material_id == material_id)
        .order_by(StockMovement.created_at.desc())
        .all()
    )


def get_history(
    db: Session,
    material_id: int | None = None,
    reason: StockMovementReason | None = None,
) -> list[StockMovement]:
    query = db.query(StockMovement)
    if material_id is not None:
        query = query.filter(StockMovement.warehouse_material_id == material_id)
    if reason is not None:
        query = query.filter(StockMovement.reason == reason)
    return query.order_by(StockMovement.created_at.desc()).all()


# --- Поставщики (задача 0011-a) ---


def get_supplier_or_404(db: Session, supplier_id: int) -> Supplier:
    supplier = db.get(Supplier, supplier_id)
    if not supplier:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Поставщик не найден")
    return supplier


def delete_supplier(db: Session, supplier: Supplier) -> None:
    """Удалить поставщика. Прайс и заметки поставщика каскадятся на уровне БД
    (`ondelete="CASCADE"`). Заказы поставщику (`app.accounting.SupplierOrder`)
    ссылаются на него без `ondelete` — отказ 409 с понятным сообщением вместо
    сырого `IntegrityError`, если такие заказы есть (0030-c)."""
    has_orders = db.query(SupplierOrder.id).filter(SupplierOrder.supplier_id == supplier.id).first() is not None
    if has_orders:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Нельзя удалить поставщика — по нему есть заказы в разделе «Бухгалтерия»",
        )
    db.delete(supplier)
    db.flush()


def _price_item_or_404(supplier: Supplier, item_id: int) -> SupplierPriceItem:
    item = next((i for i in supplier.price_items if i.id == item_id), None)
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Строка прайса не найдена")
    return item


def _clean_name(raw: str | None) -> str:
    name = (raw or "").strip()
    if not name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Название поставщика не может быть пустым"
        )
    return name


class _TierView:
    """Лёгкая обёртка над сохранённым диапазоном (dict) — чтобы
    ``_validate_price_item`` одинаково работал и со схемами, и с моделью."""

    def __init__(self, price: float | None = None, **_: object) -> None:
        self.price = price


def _validate_price_item(material: str | None, tiers) -> str:
    """Отклоняет строку прайса без материала или без единого диапазона с ценой."""
    cleaned = (material or "").strip()
    if not cleaned:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="У строки прайса не указан материал"
        )
    if not tiers or all(t.price is None for t in tiers):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="У строки прайса нужен хотя бы один диапазон партии с ценой",
        )
    return cleaned


def list_suppliers(db: Session) -> list[Supplier]:
    return db.query(Supplier).order_by(Supplier.name).all()


def create_supplier(db: Session, payload: SupplierCreate) -> Supplier:
    supplier = Supplier(
        name=_clean_name(payload.name),
        categories=list(payload.categories),
        status=payload.status,
        contacts=[c.model_dump() for c in payload.contacts],
    )
    db.add(supplier)
    db.flush()
    return supplier


def update_supplier(db: Session, supplier: Supplier, payload: SupplierUpdate) -> Supplier:
    data = payload.model_dump(exclude_unset=True)
    if "name" in data:
        supplier.name = _clean_name(data["name"])
    if "categories" in data:
        supplier.categories = list(data["categories"] or [])
    if "status" in data and data["status"] is not None:
        supplier.status = data["status"]
    if "contacts" in data:
        supplier.contacts = [c.model_dump() for c in (payload.contacts or [])]
    db.flush()
    return supplier


def add_price_item(db: Session, supplier: Supplier, payload: SupplierPriceItemCreate) -> SupplierPriceItem:
    material = _validate_price_item(payload.material, payload.tiers)
    item = SupplierPriceItem(
        supplier_id=supplier.id,
        material=material,
        category=payload.category,
        tiers=[t.model_dump() for t in payload.tiers],
        lead_time=payload.lead_time,
        round=payload.round,
    )
    db.add(item)
    db.flush()
    return item


def update_price_item(
    db: Session, supplier: Supplier, item_id: int, payload: SupplierPriceItemUpdate
) -> SupplierPriceItem:
    item = _price_item_or_404(supplier, item_id)
    data = payload.model_dump(exclude_unset=True)
    material = data["material"] if "material" in data else item.material
    tiers = payload.tiers if "tiers" in data else None
    if "material" in data or "tiers" in data:
        # проверяем итоговое состояние строки, а не только присланные поля
        check_tiers = payload.tiers if tiers is not None else [_TierView(**t) for t in item.tiers]
        item.material = _validate_price_item(material, check_tiers)
    if "category" in data:
        item.category = data["category"]
    if tiers is not None:
        item.tiers = [t.model_dump() for t in payload.tiers]
    if "lead_time" in data:
        item.lead_time = data["lead_time"]
    if "round" in data:
        item.round = data["round"]
    db.flush()
    return item


def delete_price_item(db: Session, supplier: Supplier, item_id: int) -> None:
    db.delete(_price_item_or_404(supplier, item_id))
    db.flush()


def link_max_chat(db: Session, supplier: Supplier, chat_id: int) -> Supplier:
    taken = (
        db.query(Supplier)
        .filter(Supplier.max_chat_id == chat_id, Supplier.id != supplier.id)
        .first()
    )
    if taken:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Чат уже привязан к поставщику «{taken.name}» — сначала открепите его там",
        )
    supplier.max_chat_id = chat_id
    db.flush()
    return supplier


def unlink_max_chat(db: Session, supplier: Supplier) -> Supplier:
    supplier.max_chat_id = None
    db.flush()
    return supplier


def price_for_qty(item: SupplierPriceItem, qty: float) -> float | None:
    """Цена материала у поставщика для партии размера ``qty`` — первый диапазон,
    в который она попадает. Используется заявками на материалы (production)."""
    for tier in item.tiers or []:
        low = tier.get("min_qty") or 0
        high = tier.get("max_qty")
        if qty >= low and (high is None or qty <= high):
            return tier.get("price")
    return None


_BACKFILL_FIELD_LABEL = {"category": "категория", "lead_time": "срок поставки"}


def import_price_list(
    db: Session,
    supplier: Supplier,
    headers: list[str],
    data: list[list[str]],
    mapping: ColumnMapping,
    created_by: User,
) -> tuple[int, int]:
    """Добавляет строки прайса из разобранной таблицы. Возвращает
    (добавлено, пропущено). Задачу «дозаполнить» больше не создаёт — это делает
    отдельный `create_backfill_task` по кнопке в отчёте (задача 0011-h)."""
    built = price_import.build_rows(headers, data, mapping)
    for item in built.items:
        db.add(SupplierPriceItem(supplier_id=supplier.id, **item))
    db.flush()
    return len(built.items), built.skipped


def create_backfill_task(db: Session, supplier: Supplier, missing_fields: list[str]) -> int:
    """Задача «дозаполнить прайс поставщика» — по подтверждению пользователя."""
    parts = [_BACKFILL_FIELD_LABEL.get(m, m) for m in missing_fields] or ["проверить импортированные строки"]
    detail = ", ".join(parts)
    assignees = user_service.users_with_access(db, AccessModule.WAREHOUSE)
    task = task_service.create_link_task(
        db,
        title=f"Дозаполнить прайс поставщика «{supplier.name}»: нет данных — {detail}",
        link_type=TaskLinkType.SUPPLIER_PRICE_BACKFILL,
        link_id=supplier.id,
        assignees=assignees,
        link_meta={"supplier_id": supplier.id, "missing": missing_fields},
    )
    db.flush()
    return task.id


def ai_fill_categories(db: Session, supplier: Supplier) -> tuple[int, int]:
    """ИИ проставляет `category` строкам прайса поставщика, где она пуста.
    Возвращает (проставлено, осталось пустыми). Требует ANTHROPIC_API_KEY."""
    if not price_import.ai_enabled():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ИИ недоступен: не задан ANTHROPIC_API_KEY",
        )
    blanks = [it for it in supplier.price_items if not (it.category or "").strip()]
    if not blanks:
        return 0, 0
    try:
        assigned = price_import.ai_assign_categories([(it.id, it.material) for it in blanks])
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"ИИ не смог проставить категории: {exc}"
        ) from exc
    for it in blanks:
        if it.id in assigned:
            it.category = assigned[it.id]
    db.flush()
    filled = sum(1 for it in blanks if it.id in assigned)
    return filled, len(blanks) - filled


def _require_supplier_max_chat(supplier: Supplier) -> int:
    if supplier.max_chat_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Чтобы спросить срок у поставщика, сначала привяжите к нему чат MAX",
        )
    return supplier.max_chat_id


def draft_lead_time_question(supplier: Supplier) -> dict:
    """Черновик сообщения поставщику в MAX с просьбой указать сроки поставки."""
    chat_id = _require_supplier_max_chat(supplier)
    materials = [it.material for it in supplier.price_items if not (it.lead_time or "").strip()]
    if not materials:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="У всех строк прайса уже указан срок поставки"
        )
    if not price_import.ai_enabled():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="ИИ недоступен: не задан ANTHROPIC_API_KEY"
        )
    try:
        message = price_import.ai_lead_time_message(supplier.name, materials)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"ИИ не смог составить сообщение: {exc}"
        ) from exc
    return {"message": message, "materials": materials, "chat_id": chat_id}


def send_lead_time_question(supplier: Supplier, message: str) -> int:
    """Отправляет сообщение поставщику в привязанный чат MAX. Возвращает chat_id."""
    chat_id = _require_supplier_max_chat(supplier)
    text = (message or "").strip()
    if not text:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Пустое сообщение")
    from app.max import service as max_service

    max_service.send_message(chat_id, text)
    return chat_id


def add_supplier_note(db: Session, supplier: Supplier, author_id: int, text: str) -> SupplierNote:
    cleaned = (text or "").strip()
    if not cleaned:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Текст заметки не может быть пустым"
        )
    note = SupplierNote(supplier_id=supplier.id, author_id=author_id, text=cleaned[:2000])
    db.add(note)
    db.flush()
    return note


def delete_supplier_note(db: Session, supplier: Supplier, note_id: int) -> None:
    note = next((n for n in supplier.notes if n.id == note_id), None)
    if not note:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Заметка не найдена")
    # remove из коллекции (delete-orphan) — держит supplier.notes актуальным для
    # последующего supplier_out даже без expire после commit
    supplier.notes.remove(note)
    db.flush()


def recalculate_supplier_totals(db: Session, supplier_id: int) -> None:
    """Пересчитывает `Supplier.total_ordered` как сумму `total_cost` всех его
    `SupplierOrder` (задача 0011-d). Полный пересчёт, а не инкремент/декремент —
    не разъезжается при правке/удалении заказов. Вызывается сервисом заказов
    после create/update/delete; `total_paid` эта функция не трогает (растёт
    при проведении оплаты — 0011-f)."""
    supplier = db.get(Supplier, supplier_id)
    if supplier is None:
        return
    total = (
        db.query(func.sum(SupplierOrder.total_cost))
        .filter(SupplierOrder.supplier_id == supplier_id)
        .scalar()
    )
    supplier.total_ordered = float(total or 0)
    db.flush()


def supplier_out(supplier: Supplier) -> SupplierOut:
    return SupplierOut(
        id=supplier.id,
        name=supplier.name,
        categories=list(supplier.categories or []),
        status=supplier.status,
        contacts=list(supplier.contacts or []),
        max_chat_id=supplier.max_chat_id,
        created_at=supplier.created_at,
        price_items=[SupplierPriceItemOut.model_validate(i) for i in supplier.price_items],
        price_items_count=len(supplier.price_items),
        notes=[
            SupplierNoteOut(
                id=n.id,
                supplier_id=n.supplier_id,
                author_id=n.author_id,
                author_name=(n.author.full_name if n.author else None),
                text=n.text,
                created_at=n.created_at,
            )
            for n in supplier.notes
        ],
        total_ordered=float(supplier.total_ordered or 0),
        total_paid=float(supplier.total_paid or 0),
        balance=float(supplier.total_ordered or 0) - float(supplier.total_paid or 0),
    )
