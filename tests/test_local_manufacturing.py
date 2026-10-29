from uuid import uuid4
import pytest
from fastapi import HTTPException
from app.moysklad.recipes import Recipe, Manufacturing, manufacture, preview, ManufactureRun
from app.warehouse.models import (
    WarehouseMaterial,
    Warehouse,
    MaterialCategory,
    StockMovement,
    WarehouseOperation,
)
from app.common.module_access import Module, AccessLevel


def setup(db):
    raw = WarehouseMaterial(
        warehouse=Warehouse.TECHNOLOGY,
        title="Сырьё",
        code="RAW",
        unit="м³",
        is_fractional=True,
        quantity_in_stock=1,
        category=MaterialCategory.NONE,
    )
    output = WarehouseMaterial(
        warehouse=Warehouse.TECHNOLOGY,
        title="Изделие",
        code="OUT",
        unit="шт",
        is_fractional=False,
        quantity_in_stock=0,
        category=MaterialCategory.NONE,
    )
    db.add_all([raw, output])
    db.flush()
    recipe = Recipe(
        title="Тест",
        archived=False,
        content={
            "inputs": [{"material_id": raw.id, "title": "Сырьё", "quantity": "0.0025"}],
            "outputs": [{"material_id": output.id, "title": "Изделие", "quantity": "1"}],
            "stages": [],
        },
    )
    db.add(recipe)
    db.commit()
    return raw, output, recipe


def test_fractional_atomic_release_and_idempotence(db, make_user):
    user = make_user(admin=True)
    raw, output, recipe = setup(db)
    args = Manufacturing(warehouse=Warehouse.TECHNOLOGY, multiplier=2, request_id=uuid4())
    before = preview(db, recipe, args)
    assert before["inputs"][0]["quantity"] == 0.005
    result = manufacture(db, recipe.id, args, user)
    assert float(raw.quantity_in_stock) == 0.995 and float(output.quantity_in_stock) == 2
    assert db.query(StockMovement).count() == 2 and db.query(WarehouseOperation).count() == 2
    assert manufacture(db, recipe.id, args, user) == result
    assert db.query(ManufactureRun).count() == 1
    assert result["recipe_snapshot"]["inputs"][0]["quantity"] == "0.0025"


def test_shortage_rolls_back_everything_and_archive_is_blocked(db, make_user):
    user = make_user(admin=True)
    raw, output, recipe = setup(db)
    with pytest.raises(HTTPException) as e:
        manufacture(
            db,
            recipe.id,
            Manufacturing(warehouse=Warehouse.TECHNOLOGY, multiplier=1000, request_id=uuid4()),
            user,
        )
    assert e.value.status_code == 422
    assert float(raw.quantity_in_stock) == 1 and float(output.quantity_in_stock) == 0
    assert db.query(ManufactureRun).count() == 0 and db.query(StockMovement).count() == 0
    recipe.archived = True
    db.commit()
    with pytest.raises(HTTPException) as e:
        preview(db, recipe, Manufacturing(warehouse=Warehouse.TECHNOLOGY, multiplier=1, request_id=uuid4()))
    assert e.value.status_code == 422


def test_access_requires_both_production_and_warehouse_edit(db, make_user, api):
    raw, output, recipe = setup(db)
    body = {"warehouse": Warehouse.TECHNOLOGY.value, "multiplier": 1, "request_id": str(uuid4())}
    prod = api(make_user(Module.PRODUCTION))
    assert prod.post(f"/api/moysklad/techcards/{recipe.id}/manufacture", json=body).status_code == 403
    assert prod.post(f"/api/moysklad/techcards/{recipe.id}/preview", json=body).status_code == 200
    view = api(make_user(Module.PRODUCTION, Module.WAREHOUSE, level=AccessLevel.VIEW))
    assert view.post(f"/api/moysklad/techcards/{recipe.id}/manufacture", json=body).status_code == 403
    both = api(make_user(Module.PRODUCTION, Module.WAREHOUSE))
    assert both.post(f"/api/moysklad/techcards/{recipe.id}/manufacture", json=body).status_code == 200
