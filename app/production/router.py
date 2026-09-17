from fastapi import Depends, FastAPI
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.common.module_access import Module as AccessModule
from app.core.deps import require_admin, require_module
from app.db.session import get_db
from app.production import home as production_home
from app.production import service as production_service
from app.production.models import Production, ProductionBlock
from app.cycle.models import Cycle
from app.production.schemas import (
    BlockCreate,
    BlockDependencyCreate,
    BlockMaterialCreate,
    BlockMaterialOut,
    BlockMaterialUpdate,
    BlockOut,
    BlockUpdate,
    MaterialRequestCreate,
    MaterialRequestOut,
    ProductionHomeOut,
    ProductionOut,
    ProductionListOut,
)
from app.users.models import User

app = FastAPI(
    title="Soborbum — Производство",
    description="Блоки производства, их задачи и необходимые материалы, запросы материалов со склада.",
    version="0.4.0",
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
    counts = (db.query(ProductionBlock.production_id, func.count(ProductionBlock.id).label("count"))
              .group_by(ProductionBlock.production_id).subquery())
    query = (db.query(Production, Cycle.status, func.coalesce(counts.c.count, 0))
             .join(Cycle, Cycle.id == Production.cycle_id)
             .outerjoin(counts, counts.c.production_id == Production.id))
    if cycle_id is not None:
        query = query.filter(Production.cycle_id == cycle_id)
    rows = query.order_by(Production.cycle_id.desc(), Production.house_index.asc()).all()
    return [ProductionListOut(id=p.id, cycle_id=p.cycle_id, house_index=p.house_index, name=p.name,
                              cycle_status=cycle_status, created_at=p.created_at, block_count=count)
            for p, cycle_status, count in rows]


@app.get("/{production_id}", response_model=ProductionOut)
def get_production(production_id: int, db: Session = Depends(get_db), _: User = Depends(require_production)):
    return production_service.get_production_or_404(db, production_id)


@app.get("/{production_id}/home", response_model=ProductionHomeOut)
def get_production_home(
    production_id: int, db: Session = Depends(get_db), _: User = Depends(require_production)
):
    production = production_service.get_production_or_404(db, production_id)
    return production_home.build_home(db, production)


@app.delete("/{production_id}", status_code=204)
def delete_production(production_id: int, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    production = production_service.get_production_or_404(db, production_id)
    production_service.delete_production(db, production)
    db.commit()


@app.post("/{production_id}/blocks", response_model=BlockOut, status_code=201)
def create_block(
    production_id: int,
    payload: BlockCreate,
    db: Session = Depends(get_db),
    _: User = Depends(require_production),
):
    block = production_service.create_block(db, production_id, payload)
    db.commit()
    db.refresh(block)
    return block


@app.get("/blocks/{block_id}", response_model=BlockOut)
def get_block(block_id: int, db: Session = Depends(get_db), _: User = Depends(require_production)):
    return production_service.get_block_or_404(db, block_id)


@app.patch("/blocks/{block_id}", response_model=BlockOut)
def update_block(
    block_id: int,
    payload: BlockUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_production),
):
    block = production_service.get_block_or_404(db, block_id)
    block = production_service.update_block(db, block, payload)
    db.commit()
    db.refresh(block)
    return block


@app.delete("/blocks/{block_id}", status_code=204)
def delete_block(block_id: int, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    block = production_service.get_block_or_404(db, block_id)
    production_service.delete_block(db, block)
    db.commit()


@app.post("/blocks/{block_id}/dependencies", response_model=BlockOut, status_code=201)
def add_block_dependency(
    block_id: int,
    payload: BlockDependencyCreate,
    db: Session = Depends(get_db),
    _: User = Depends(require_production),
):
    block = production_service.get_block_or_404(db, block_id)
    block = production_service.add_block_dependency(db, block, payload.depends_on_id)
    db.commit()
    db.refresh(block)
    return block


@app.delete("/blocks/{block_id}/dependencies/{depends_on_id}", response_model=BlockOut)
def remove_block_dependency(
    block_id: int,
    depends_on_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_production),
):
    block = production_service.get_block_or_404(db, block_id)
    block = production_service.remove_block_dependency(db, block, depends_on_id)
    db.commit()
    db.refresh(block)
    return block


@app.post("/blocks/{block_id}/materials", response_model=BlockMaterialOut, status_code=201)
def add_block_material(
    block_id: int,
    payload: BlockMaterialCreate,
    db: Session = Depends(get_db),
    _: User = Depends(require_production),
):
    material = production_service.add_block_material(db, block_id, payload)
    db.commit()
    db.refresh(material)
    return material


@app.patch("/block-materials/{material_id}", response_model=BlockMaterialOut)
def update_block_material(
    material_id: int,
    payload: BlockMaterialUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_production),
):
    material = production_service.get_block_material_or_404(db, material_id)
    material = production_service.update_required_quantity(db, material, payload.quantity_required, user)
    db.commit()
    db.refresh(material)
    return material


@app.post("/block-materials/{material_id}/request", response_model=MaterialRequestOut, status_code=201)
def request_material(
    material_id: int,
    payload: MaterialRequestCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_production),
):
    material = production_service.get_block_material_or_404(db, material_id)
    request = production_service.request_material(db, material, payload.quantity, user)
    db.commit()
    db.refresh(request)
    return request
