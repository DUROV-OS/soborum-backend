"""Atomic transfers and counts. A stale count never overwrites intervening stock work."""

from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import hashlib
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text, func
from sqlalchemy.orm import Session

from app.common.module_access import Module
from app.core.deps import require_view, require_edit, require_full
from app.db.session import get_db
from app.users.models import User
from app.warehouse.models import (
    Warehouse,
    WarehouseMaterial,
    WarehouseOperation,
    WarehouseOperationKind,
    StockDocument,
    StockMovementReason,
    StockMovement,
    STOCK_IN_REASONS,
    STOCK_OUT_REASONS,
)
from app.warehouse import service

router = APIRouter(prefix="/documents")
view, edit, full = (
    require_view(Module.WAREHOUSE),
    require_edit(Module.WAREHOUSE),
    require_full(Module.WAREHOUSE),
)
MAX_QTY = Decimal("999999999999.999999")


class Line(BaseModel):
    model_config = ConfigDict(extra="forbid")
    material_id: int = Field(gt=0)
    quantity: Decimal | None = Field(
        default=None, ge=0, le=MAX_QTY, decimal_places=6, allow_inf_nan=False
    )


class Create(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    kind: Literal["transfer", "inventory"]
    warehouse: Warehouse
    target_warehouse: Warehouse | None = None
    occurred_at: datetime | None = None
    note: str = Field(default="", max_length=1000)
    lines: list[Line] = Field(min_length=1, max_length=5000)


class Change(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)
    note: str = Field(max_length=1000)
    lines: list[Line] = Field(min_length=1, max_length=5000)


class Version(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)


class Post(Version):
    request_id: UUID


class Archive(Version):
    archived: bool


def dec(value):
    return Decimal(str(value or 0))


def fail(message, code=409):
    raise HTTPException(code, message)


def advisory(db, key):
    if db.bind.dialect.name == "postgresql":
        number = int.from_bytes(
            hashlib.sha256(key.encode()).digest()[:8], "big", signed=True
        )
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": number})


def get(db, id, lock=False):
    stmt = db.query(StockDocument).filter_by(id=id)
    if lock:
        stmt = stmt.populate_existing().with_for_update(of=StockDocument)
    row = stmt.first()
    if row is None:
        fail("Документ не найден", 404)
    return row


def out(row):
    result = {
        name: getattr(row, name)
        for name in (
            "id",
            "kind",
            "status",
            "number",
            "warehouse",
            "target_warehouse",
            "occurred_at",
            "note",
            "lines",
            "operation_ids",
            "source_id",
            "source_state",
            "archived",
            "version",
            "actor_id",
            "posted_by_id",
            "posted_at",
            "created_at",
        )
    }
    for key in ("occurred_at", "created_at", "posted_at"):
        value = result[key]
        if value is not None and value.tzinfo is None:
            result[key] = value.replace(tzinfo=timezone.utc)
    return result


def draft(row, version):
    if row.archived or row.status != "draft" or row.source_id:
        fail("Изменять можно только действующий черновик")
    if row.version != version:
        fail("Документ изменён. Откройте его заново.")


def snapshot(db, row, lines, *, preserve=True):
    if row.kind == "transfer" and len(lines) > 200:
        fail("В одном перемещении допускается до 200 позиций", 422)
    previous = {l["material_id"]: l for l in row.lines or []} if preserve else {}
    ids = [l.material_id for l in lines]
    if len(set(ids)) != len(ids):
        fail("Объедините повторяющиеся материалы в одну строку", 422)
    materials = service._lock_materials(db, ids)
    watermarks = movement_watermarks(db, ids)
    result = []
    for line in lines:
        m = materials.get(line.material_id)
        if not m or m.is_archived or m.warehouse.value != row.warehouse:
            fail("Выберите действующие материалы одного склада", 422)
        if line.quantity is not None and (
            not m.is_fractional and line.quantity != line.quantity.to_integral_value()
        ):
            fail(f"«{m.title}» учитывается только в целых {m.unit}", 422)
        if row.kind == "transfer" and (line.quantity is None or line.quantity <= 0):
            fail("Количество перемещения должно быть больше нуля", 422)
        old = previous.get(m.id)
        # Retain the original baseline until an explicit re-count resets every actual quantity.
        result.append(
            {
                "material_id": m.id,
                "title": m.title,
                "code": m.code,
                "unit": m.unit,
                "is_fractional": m.is_fractional,
                "expected_quantity": (
                    old["expected_quantity"] if old else str(dec(m.quantity_in_stock))
                ),
                "expected_movement_id": (
                    old.get("expected_movement_id", 0)
                    if old
                    else watermarks.get(m.id, 0)
                ),
                "quantity": str(line.quantity) if line.quantity is not None else None,
            }
        )
    return result


def movement_watermarks(db, ids):
    return dict(
        db.query(StockMovement.warehouse_material_id, func.max(StockMovement.id))
        .filter(
            StockMovement.warehouse_material_id.in_(ids),
            StockMovement.reason.in_(STOCK_IN_REASONS + STOCK_OUT_REASONS),
        )
        .group_by(StockMovement.warehouse_material_id)
        .all()
    )


@router.get("")
def listing(
    kind: Literal["transfer", "inventory"] | None = None,
    archive: Literal["active", "archived", "all"] = "active",
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(view),
):
    query = db.query(StockDocument)
    if kind:
        query = query.filter_by(kind=kind)
    if archive != "all":
        query = query.filter_by(archived=archive == "archived")
    return {
        "total": query.count(),
        "items": [
            out(r)
            for r in query.order_by(
                StockDocument.occurred_at.desc(), StockDocument.id.desc()
            )
            .offset(offset)
            .limit(limit)
        ],
    }


@router.get("/{id}")
def detail(id: int, db: Session = Depends(get_db), user: User = Depends(view)):
    return out(get(db, id))


@router.post("", status_code=201)
def create(body: Create, db: Session = Depends(get_db), user: User = Depends(edit)):
    key = str(body.request_id)
    parameters = body.model_dump(mode="json")
    advisory(db, "stock-create:" + key)
    prior = db.query(StockDocument).filter_by(request_id=key).first()
    if prior:
        if prior.actor_id != user.id or prior.parameters != parameters:
            fail("Этот номер запроса уже использован с другими данными")
        return out(prior)
    if body.kind == "transfer" and (
        not body.target_warehouse or body.target_warehouse == body.warehouse
    ):
        fail("Выберите другой склад назначения", 422)
    if body.kind == "inventory" and body.target_warehouse:
        fail("Инвентаризация проводится на одном складе", 422)
    row = StockDocument(
        kind=body.kind,
        status="draft",
        number="",
        warehouse=body.warehouse.value,
        target_warehouse=body.target_warehouse.value if body.target_warehouse else None,
        occurred_at=service.resolve_occurred_at(body.occurred_at),
        note=body.note.strip(),
        lines=[],
        operation_ids=[],
        actor_id=user.id,
        request_id=key,
        parameters=parameters,
        version=1,
        archived=False,
    )
    db.add(row)
    row.lines = snapshot(db, row, body.lines)
    db.flush()
    row.number = ("ПМ-" if row.kind == "transfer" else "ИН-") + str(row.id)
    db.commit()
    return out(row)


@router.patch("/{id}")
def change(
    id: int, body: Change, db: Session = Depends(get_db), user: User = Depends(edit)
):
    row = get(db, id, True)
    draft(row, body.version)
    row.lines = snapshot(db, row, body.lines)
    row.note = body.note.strip()
    row.version += 1
    db.commit()
    return out(row)


@router.post("/{id}/recount")
def recount(
    id: int, body: Version, db: Session = Depends(get_db), user: User = Depends(edit)
):
    row = get(db, id, True)
    draft(row, body.version)
    if row.kind != "inventory":
        fail("Пересчёт доступен для инвентаризации")
    row.lines = snapshot(
        db, row, [Line(material_id=l["material_id"]) for l in row.lines], preserve=False
    )
    row.version += 1
    db.commit()
    return out(row)


def identity(db, material):
    from app.moysklad.models import SourceLink

    return {
        l.source_key.split("/")[1]
        for l in db.query(SourceLink).filter_by(
            target_type="warehouse_materials", target_id=material.id
        )
        if l.source_key.startswith("stock/")
    }


def resolve_target(db, source, warehouse):
    """Source identity takes precedence over locally editable codes and names."""
    from app.moysklad.models import SourceLink

    source_ids = identity(db, source)
    candidates = []
    if source_ids:
        for sid in sorted(source_ids):
            candidates.extend(
                db.query(WarehouseMaterial)
                .join(SourceLink, SourceLink.target_id == WarehouseMaterial.id)
                .filter(
                    SourceLink.target_type == "warehouse_materials",
                    SourceLink.source_key.startswith(
                        "stock/" + sid + "/", autoescape=True
                    ),
                    WarehouseMaterial.warehouse == warehouse,
                )
                .all()
            )
        candidates = list({m.id: m for m in candidates}.values())
    else:
        candidates = (
            db.query(WarehouseMaterial)
            .filter_by(warehouse=warehouse, code=source.code)
            .all()
        )
    if len(candidates) > 1:
        fail("Неоднозначная карточка материала на складе назначения")
    if candidates:
        target = candidates[0]
        if (
            target.is_archived
            or target.unit != source.unit
            or target.is_fractional != source.is_fractional
        ):
            fail("На складе назначения архивная карточка или другие единицы учёта")
        if not source_ids and (
            identity(db, target)
            or target.title.strip().casefold() != source.title.strip().casefold()
        ):
            fail("Код на складе назначения занят другим материалом")
        return target
    if source_ids:
        fail(
            "Не найдена связанная карточка на складе назначения. Проверьте сопоставление."
        )
    target = WarehouseMaterial(
        warehouse=warehouse,
        code=source.code,
        title=source.title,
        category=source.category,
        catalog_path=source.catalog_path,
        unit=source.unit,
        is_fractional=source.is_fractional,
        quantity_in_stock=0,
        purchase_price=source.purchase_price,
        threshold=0,
        supplier_id=source.supplier_id,
        kind=source.kind,
        size=source.size,
        diameter=source.diameter,
        serial_number=source.serial_number,
        pack_quantity=source.pack_quantity,
        is_archived=False,
    )
    db.add(target)
    db.flush()
    return target


def operation(db, row, kind, user):
    result = WarehouseOperation(
        kind=kind,
        occurred_at=row.occurred_at,
        destination=(
            (
                row.warehouse
                if kind == WarehouseOperationKind.TRANSFER_IN
                else row.target_warehouse
            )
            if row.kind == "transfer"
            else None
        ),
        note=(
            f"{'Перемещение' if row.kind == 'transfer' else 'Инвентаризация'} № {row.number}. "
            + (
                f"{row.warehouse} → {row.target_warehouse}. "
                if row.kind == "transfer"
                else ""
            )
            + row.note
        )[:500],
        created_by_id=user.id,
    )
    db.add(result)
    db.flush()
    return result


def record(db, material, delta, reason, user, op):
    material.quantity_in_stock = dec(material.quantity_in_stock) + delta
    db.flush()
    service.log_movement(
        db, material, float(delta), reason, user, note=op.note, operation_id=op.id
    )


def post_document(db, row, body, user):
    if row.kind == "inventory":
        full(user)
    if (
        row.status == "posted"
        and row.post_request_id == str(body.request_id)
        and row.posted_by_id == user.id
    ):
        return row
    draft(row, body.version)
    if any(l.get("quantity") is None for l in row.lines):
        fail("Заполните фактическое количество во всех строках", 422)
    if not row.note.strip():
        fail("Укажите основание операции", 422)
    originals = {
        m.id: m
        for m in db.query(WarehouseMaterial).filter(
            WarehouseMaterial.id.in_([l["material_id"] for l in row.lines])
        )
    }
    if len(originals) != len(row.lines):
        fail("Карточка материала удалена")
    targets = {}
    if row.kind == "transfer":
        # Serialize missing destination-card creation, then acquire every stock lock in ID order.
        for code in sorted({m.code for m in originals.values()}):
            advisory(db, "stock-target:" + row.target_warehouse + ":" + code)
        targets = {
            m.id: resolve_target(db, m, Warehouse(row.target_warehouse))
            for m in originals.values()
        }
        if len({m.id for m in targets.values()}) != len(targets):
            fail("Несколько строк сопоставлены с одной карточкой назначения")
    locked = service._lock_materials(
        db, list(originals) + [m.id for m in targets.values()]
    )
    if set(locked) != set(originals) | {m.id for m in targets.values()}:
        fail("Карточка материала удалена. Проверьте строки документа.")
    watermarks = (
        movement_watermarks(db, list(originals)) if row.kind == "inventory" else {}
    )
    for line in row.lines:
        m = locked[line["material_id"]]
        if (
            m.is_archived
            or m.warehouse.value != row.warehouse
            or m.code != line["code"]
            or m.unit != line["unit"]
            or m.is_fractional != line["is_fractional"]
        ):
            fail("Карточка материала изменилась. Проверьте строки документа.")
        qty = dec(line["quantity"])
        if not m.is_fractional and qty != qty.to_integral_value():
            fail("Для штучного материала укажите целое количество", 422)
        if row.kind == "transfer" and (qty <= 0 or dec(m.quantity_in_stock) < qty):
            fail("Недостаточно остатка для перемещения")
        if row.kind == "inventory" and (
            dec(m.quantity_in_stock) != dec(line["expected_quantity"])
            or watermarks.get(m.id, 0) != line.get("expected_movement_id", 0)
        ):
            fail(
                "После начала пересчёта остатки изменились. Переснимите остатки и пересчитайте материал."
            )
        if row.kind == "transfer":
            t = locked[targets[m.id].id]
            if (
                t.is_archived
                or t.unit != m.unit
                or t.is_fractional != m.is_fractional
                or t.warehouse.value != row.target_warehouse
            ):
                fail("Карточка назначения изменилась. Проверьте документ.")
            if not identity(db, m) and (
                t.code != m.code
                or t.title.strip().casefold() != m.title.strip().casefold()
            ):
                fail("Карточки перемещения больше не соответствуют одному материалу")
            if dec(t.quantity_in_stock) < 0 or dec(t.quantity_in_stock) + qty > MAX_QTY:
                fail("Проверьте остаток склада назначения")
    ids = []
    lines = []
    if row.kind == "transfer":
        outgoing = operation(db, row, WarehouseOperationKind.TRANSFER_OUT, user)
        incoming = operation(db, row, WarehouseOperationKind.TRANSFER_IN, user)
        ids = [outgoing.id, incoming.id]
        for line in row.lines:
            m = locked[line["material_id"]]
            t = locked[targets[m.id].id]
            qty, old = dec(line["quantity"]), dec(t.quantity_in_stock)
            price = dec(m.purchase_price)
            t.purchase_price = (
                (old * dec(t.purchase_price) + qty * price) / (old + qty)
            ).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
            record(db, m, -qty, StockMovementReason.TRANSFER_OUT, user, outgoing)
            record(db, t, qty, StockMovementReason.TRANSFER_IN, user, incoming)
            lines.append(
                {
                    **line,
                    "target_material_id": t.id,
                    "unit_price": str(price),
                    "source_balance_after": str(dec(m.quantity_in_stock)),
                    "target_balance_after": str(dec(t.quantity_in_stock)),
                }
            )
    else:
        ops = {}
        for line in row.lines:
            m = locked[line["material_id"]]
            delta = dec(line["quantity"]) - dec(m.quantity_in_stock)
            if delta:
                kind = (
                    WarehouseOperationKind.COUNT_GAIN
                    if delta > 0
                    else WarehouseOperationKind.COUNT_LOSS
                )
                if kind not in ops:
                    ops[kind] = operation(db, row, kind, user)
                    ids.append(ops[kind].id)
                reason = (
                    StockMovementReason.COUNT_GAIN
                    if delta > 0
                    else StockMovementReason.COUNT_LOSS
                )
                record(db, m, delta, reason, user, ops[kind])
            lines.append(
                {
                    **line,
                    "delta": str(delta),
                    "unit_price": str(dec(m.purchase_price)),
                    "source_balance_after": str(dec(m.quantity_in_stock)),
                }
            )
    for m in locked.values():
        service.sync_shortage_task(db, m)
    row.lines, row.operation_ids = lines, ids
    row.status, row.post_request_id = "posted", str(body.request_id)
    row.posted_by_id, row.posted_at = user.id, datetime.now(timezone.utc)
    row.version += 1
    db.flush()
    return row


@router.post("/{id}/post")
def post(
    id: int, body: Post, db: Session = Depends(get_db), user: User = Depends(edit)
):
    advisory(db, "stock-post:" + str(body.request_id))
    prior = (
        db.query(StockDocument).filter_by(post_request_id=str(body.request_id)).first()
    )
    if prior and (prior.id != id or prior.posted_by_id != user.id):
        fail("Этот номер запроса уже использован для другого проведения")
    row = post_document(db, get(db, id, True), body, user)
    db.commit()
    return out(row)


@router.post("/{id}/archive")
def archive(
    id: int, body: Archive, db: Session = Depends(get_db), user: User = Depends(full)
):
    row = get(db, id, True)
    if row.version != body.version:
        fail("Документ изменён. Откройте его заново.")
    row.archived = body.archived
    row.version += 1
    db.commit()
    return out(row)
