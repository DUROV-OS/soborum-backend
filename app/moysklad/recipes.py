"""Local recipes and atomic stock transformations. No upstream calls."""

from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID
from pydantic import BaseModel, Field, ConfigDict, model_validator
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import String, JSON, ForeignKey, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column, Session
from sqlalchemy.exc import IntegrityError
from app.db.base import Base
from app.db.session import get_db
from app.common.module_access import Module
from app.core.deps import get_current_user, require_edit, require_full
from app.users.models import User
from app.warehouse.models import (
    Warehouse,
    WarehouseMaterial,
    WarehouseOperation,
    WarehouseOperationKind,
    IssueDestinationKind,
    StockMovement,
    StockMovementReason,
)
from app.moysklad.models import SourceLink


class Recipe(Base):
    __tablename__ = "local_recipes"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    title: Mapped[str] = mapped_column(String(255))
    archived: Mapped[bool] = mapped_column(default=False)
    content: Mapped[dict] = mapped_column(JSON)
    version: Mapped[int] = mapped_column(default=1)


class ManufactureRun(Base):
    __tablename__ = "manufacture_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[str] = mapped_column(String(36), unique=True)
    recipe_id: Mapped[int] = mapped_column(ForeignKey("local_recipes.id"))
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    parameters: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RecipeLine(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str | None = Field(None, max_length=255)
    material_id: int | None = None
    title: str = Field("", max_length=255)
    unit: str = Field("", max_length=32)
    quantity: Decimal = Field(gt=0, le=10000000, max_digits=17, decimal_places=6)

    @model_validator(mode="after")
    def require_material(self):
        if not self.source_id and not self.material_id:
            raise ValueError("Выберите позицию из каталога склада")
        return self


class RecipeContent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    inputs: list[RecipeLine] = Field(default_factory=list, max_length=1000)
    outputs: list[RecipeLine] = Field(default_factory=list, max_length=100)
    stages: list[dict] = Field(default_factory=list, max_length=1000)
    warnings: list[str] = Field(default_factory=list, max_length=1000)


class RecipeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=255)
    archived: bool = False
    content: RecipeContent
    version: int | None = None


class Manufacturing(BaseModel):
    model_config = ConfigDict(extra="forbid")
    warehouse: Warehouse
    multiplier: Decimal = Field(gt=0, le=100000, max_digits=15, decimal_places=6)
    request_id: UUID


def recipe_out(row):
    return {
        "id": row.id,
        "title": row.title,
        "archived": row.archived,
        "content": row.content,
        "version": row.version,
    }


def can_view(user: User = Depends(get_current_user)):
    if not (user.has_access(Module.PRODUCTION) or user.has_access(Module.WAREHOUSE)):
        raise HTTPException(403, "Нет доступа к техкартам")
    return user


def get_recipe(db, id):
    row = db.get(Recipe, id)
    if row is None:
        raise HTTPException(404, "Техкарта не найдена")
    return row


def resolve_lines(db, recipe, warehouse, multiplier):
    if recipe.archived:
        raise HTTPException(422, "Техкарта находится в архиве")
    content = RecipeContent.model_validate(recipe.content)
    if content.warnings:
        raise HTTPException(422, "Техкарта требует сверки: " + "; ".join(content.warnings))
    if not content.inputs or not content.outputs:
        raise HTTPException(422, "Заполните материалы и продукцию техкарты перед выпуском")

    def resolve(line):
        material = db.get(WarehouseMaterial, line.material_id) if line.material_id else None
        if material is None and line.source_id:
            link = (
                db.query(SourceLink)
                .filter_by(target_type="warehouse_materials")
                .filter(SourceLink.source_key.startswith("stock/" + line.source_id + "/", autoescape=True))
                .all()
            )
            candidates = [db.get(WarehouseMaterial, l.target_id) for l in link]
            candidates = [m for m in candidates if m and m.warehouse == warehouse]
            material = candidates[0] if len(candidates) == 1 else None
        if not material or material.warehouse != warehouse:
            raise HTTPException(422, "Сопоставьте со складом позицию: " + line.title)
        if material.is_archived:
            raise HTTPException(422, "Материал в архиве: " + material.title)
        quantity = line.quantity * multiplier
        if quantity != quantity.quantize(Decimal("0.000001")):
            raise HTTPException(422, "Количество должно быть кратно 0,000001: " + material.title)
        if not material.is_fractional and quantity != quantity.to_integral_value():
            raise HTTPException(422, "Нужно целое количество: " + material.title)
        return material, quantity

    return [resolve(line) for line in content.inputs], [resolve(line) for line in content.outputs]


def preview(db, recipe, parameters):
    inputs, outputs = resolve_lines(db, recipe, parameters.warehouse, parameters.multiplier)

    def grouped(lines):
        total = {}
        for m, q in lines:
            if m.id not in total:
                total[m.id] = [m, Decimal(0)]
            total[m.id][1] += q
        return total.values()

    return {
        "recipe_id": recipe.id,
        "inputs": [
            {
                "id": m.id,
                "title": m.title,
                "quantity": float(q),
                "unit": m.unit,
                "stock": float(m.quantity_in_stock),
                "enough": Decimal(str(m.quantity_in_stock)) >= q,
            }
            for m, q in grouped(inputs)
        ],
        "outputs": [
            {"id": m.id, "title": m.title, "quantity": float(q), "unit": m.unit} for m, q in grouped(outputs)
        ],
    }


def manufacture(db, recipe_id, parameters, user):
    key = str(parameters.request_id)
    params = parameters.model_dump(mode="json")
    params.pop("request_id")
    params["multiplier"] = str(parameters.multiplier.normalize())
    get_recipe(db, recipe_id)

    def existing():
        old = db.query(ManufactureRun).filter_by(request_id=key).first()
        if old:
            if old.actor_id != user.id or old.recipe_id != recipe_id or old.parameters != params:
                raise HTTPException(409, "Идентификатор уже использован для другой операции")
            return old.result

    old = existing()
    if old is not None:
        return old
    try:
        run = ManufactureRun(
            request_id=key, recipe_id=recipe_id, actor_id=user.id, parameters=params, result={}
        )
        db.add(run)
        db.flush()
    except IntegrityError:
        db.rollback()
        old = existing()
        if old is None:
            raise
        return old
    try:
        recipe = db.query(Recipe).filter_by(id=recipe_id).populate_existing().with_for_update().one_or_none()
        if not recipe:
            raise HTTPException(404, "Техкарта не найдена")
        inputs, outputs = resolve_lines(db, recipe, parameters.warehouse, parameters.multiplier)
        ids = sorted({m.id for m, q in inputs + outputs})
        materials = {
            m.id: m
            for m in db.query(WarehouseMaterial)
            .filter(WarehouseMaterial.id.in_(ids))
            .order_by(WarehouseMaterial.id)
            .populate_existing()
            .with_for_update()
        }
        if any(m.is_archived for m in materials.values()):
            raise HTTPException(422, "В составе есть архивный материал")
        required = {}
        for m, q in inputs:
            required[m.id] = required.get(m.id, Decimal(0)) + q
        missing = [
            materials[id].title
            for id, q in required.items()
            if Decimal(str(materials[id].quantity_in_stock)) < q
        ]
        if missing:
            raise HTTPException(422, "Не хватает материалов: " + ", ".join(missing))
        now = datetime.now(timezone.utc)
        issue = WarehouseOperation(
            kind=WarehouseOperationKind.ISSUE_MANUAL,
            occurred_at=now,
            destination_kind=IssueDestinationKind.WORKSHOP,
            destination="Выпуск по техкарте «" + recipe.title[:180] + "»",
            created_by_id=user.id,
            note="Производственный выпуск #" + str(run.id),
        )
        receipt = WarehouseOperation(
            kind=WarehouseOperationKind.RECEIPT,
            occurred_at=now,
            created_by_id=user.id,
            note="Продукция по техкарте «" + recipe.title[:180] + "»; выпуск #" + str(run.id),
        )
        db.add_all([issue, receipt])
        db.flush()
        for lines, sign, operation, reason in [
            (inputs, -1, issue, StockMovementReason.ISSUED_MANUAL),
            (outputs, 1, receipt, StockMovementReason.RECEIPT),
        ]:
            for original, q in lines:
                m = materials[original.id]
                m.quantity_in_stock = Decimal(str(m.quantity_in_stock)) + sign * q
                db.add(
                    StockMovement(
                        warehouse_material_id=m.id,
                        delta=sign * q,
                        reason=reason,
                        operation_id=operation.id,
                        balance_after=m.quantity_in_stock,
                        created_by_id=user.id,
                        note="Техкарта " + str(recipe.id),
                    )
                )
        run.result = {
            "run_id": run.id,
            "issue_operation_id": issue.id,
            "receipt_operation_id": receipt.id,
            "recipe_id": recipe.id,
            "recipe_version": recipe.version,
            "recipe_title": recipe.title,
            "recipe_snapshot": recipe.content,
            "inputs": [{"id": m.id, "quantity": float(q)} for m, q in inputs],
            "outputs": [{"id": m.id, "quantity": float(q)} for m, q in outputs],
        }
        db.commit()
        return run.result
    except Exception:
        db.rollback()
        raise


router = APIRouter(prefix="/techcards")


@router.get("")
def list_recipes(
    query: str = Query("", max_length=255),
    archive: str = Query("active", pattern="^(active|archived|all)$"),
    db: Session = Depends(get_db),
    user: User = Depends(can_view),
):
    stmt = db.query(Recipe)
    if archive != "all":
        stmt = stmt.filter(Recipe.archived.is_(archive == "archived"))
    if query:
        stmt = stmt.filter(
            Recipe.title.ilike("%" + query.replace("%", "\\%").replace("_", "\\_") + "%", escape="\\")
        )
    return [recipe_out(r) for r in stmt.order_by(Recipe.title)]


@router.post("", status_code=201)
def create_recipe(
    body: RecipeInput, db: Session = Depends(get_db), user: User = Depends(require_edit(Module.PRODUCTION))
):
    if not body.title.strip():
        raise HTTPException(422, "Введите название")
    row = Recipe(
        title=body.title.strip(), archived=body.archived, content=body.content.model_dump(mode="json")
    )
    db.add(row)
    db.commit()
    return recipe_out(row)


@router.patch("/{id}")
def update_recipe(
    id: int,
    body: RecipeInput,
    db: Session = Depends(get_db),
    user: User = Depends(require_edit(Module.PRODUCTION)),
):
    row = db.query(Recipe).filter_by(id=id).populate_existing().with_for_update().one_or_none()
    if not row:
        raise HTTPException(404, "Техкарта не найдена")
    if body.version != row.version:
        raise HTTPException(409, "Техкарта уже изменена. Обновите карточку.")
    if body.archived != row.archived:
        require_full(Module.PRODUCTION)(user)
    if not body.title.strip():
        raise HTTPException(422, "Введите название")
    row.title = body.title.strip()
    row.archived = body.archived
    row.content = body.content.model_dump(mode="json")
    row.version += 1
    db.commit()
    return recipe_out(row)


@router.post("/{id}/preview")
def preview_recipe(
    id: int, body: Manufacturing, db: Session = Depends(get_db), user: User = Depends(can_view)
):
    return preview(db, get_recipe(db, id), body)


@router.post("/{id}/manufacture")
def produce(
    id: int,
    body: Manufacturing,
    db: Session = Depends(get_db),
    user: User = Depends(require_edit(Module.PRODUCTION)),
    warehouse_user: User = Depends(require_edit(Module.WAREHOUSE)),
):
    return manufacture(db, id, body, user)
