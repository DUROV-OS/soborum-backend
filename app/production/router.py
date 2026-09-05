from fastapi import Depends, FastAPI
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.common.module_access import Module as AccessModule
from app.core.deps import require_module
from app.db.session import get_db
from app.production import service as production_service
from app.production.models import Production, ProductionModule
from app.cycle.models import Cycle
from app.production.schemas import (
    MaterialRequestCreate,
    MaterialRequestOut,
    ModuleCreate,
    ModuleMaterialCreate,
    ModuleMaterialOut,
    ModuleMaterialUpdate,
    ModuleOut,
    ModuleUpdate,
    ProductionOut,
    ProductionListOut,
)
from app.users.models import User

app = FastAPI(
    title="Soborbum — Производство",
    description="Модули дома, их задачи и необходимые материалы, запросы материалов со склада.",
    version="0.3.0",
)

require_production = require_module(AccessModule.PRODUCTION)


@app.get("/", response_model=list[ProductionListOut])
def list_productions(
    cycle_id: int | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(require_production),
):
    # A production grant must not require a cycle grant or reveal a customer's passport/prices.
    # ?cycle_id= narrows to the houses of one order, kept in house order.
    counts = (db.query(ProductionModule.production_id, func.count(ProductionModule.id).label("count"))
              .group_by(ProductionModule.production_id).subquery())
    query = (db.query(Production, Cycle.status, func.coalesce(counts.c.count, 0))
             .join(Cycle, Cycle.id == Production.cycle_id)
             .outerjoin(counts, counts.c.production_id == Production.id))
    if cycle_id is not None:
        query = query.filter(Production.cycle_id == cycle_id)
    rows = query.order_by(Production.cycle_id.desc(), Production.house_index.asc()).all()
    return [ProductionListOut(id=p.id, cycle_id=p.cycle_id, house_index=p.house_index, name=p.name,
                              cycle_status=cycle_status, created_at=p.created_at, module_count=count)
            for p, cycle_status, count in rows]


@app.get("/{production_id}", response_model=ProductionOut)
def get_production(production_id: int, db: Session = Depends(get_db), _: User = Depends(require_production)):
    return production_service.get_production_or_404(db, production_id)


@app.post("/{production_id}/modules", response_model=ModuleOut, status_code=201)
def create_module(
    production_id: int,
    payload: ModuleCreate,
    db: Session = Depends(get_db),
    _: User = Depends(require_production),
):
    module = production_service.create_module(db, production_id, payload)
    db.commit()
    db.refresh(module)
    return module


@app.get("/modules/{module_id}", response_model=ModuleOut)
def get_module(module_id: int, db: Session = Depends(get_db), _: User = Depends(require_production)):
    return production_service.get_module_or_404(db, module_id)


@app.patch("/modules/{module_id}", response_model=ModuleOut)
def update_module(
    module_id: int,
    payload: ModuleUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_production),
):
    module = production_service.get_module_or_404(db, module_id)
    module = production_service.update_module(db, module, payload)
    db.commit()
    db.refresh(module)
    return module


@app.post("/modules/{module_id}/materials", response_model=ModuleMaterialOut, status_code=201)
def add_module_material(
    module_id: int,
    payload: ModuleMaterialCreate,
    db: Session = Depends(get_db),
    _: User = Depends(require_production),
):
    material = production_service.add_module_material(db, module_id, payload)
    db.commit()
    db.refresh(material)
    return material


@app.patch("/module-materials/{material_id}", response_model=ModuleMaterialOut)
def update_module_material(
    material_id: int,
    payload: ModuleMaterialUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_production),
):
    material = production_service.get_module_material_or_404(db, material_id)
    material = production_service.update_required_quantity(db, material, payload.quantity_required, user)
    db.commit()
    db.refresh(material)
    return material


@app.post("/module-materials/{material_id}/request", response_model=MaterialRequestOut, status_code=201)
def request_material(
    material_id: int,
    payload: MaterialRequestCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_production),
):
    material = production_service.get_module_material_or_404(db, material_id)
    request = production_service.request_material(db, material, payload.quantity, user)
    db.commit()
    db.refresh(request)
    return request
