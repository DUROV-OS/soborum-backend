from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.production.models import MaterialRequestStatus
from app.cycle.models import CycleStatus


class ModuleCreate(BaseModel):
    name: str
    description: str | None = None


class ModuleUpdate(BaseModel):
    name: str | None = None
    description: str | None = None


class ModuleMaterialCreate(BaseModel):
    warehouse_material_id: int
    inventory_number: str
    unit: str
    quantity_required: float


class ModuleMaterialUpdate(BaseModel):
    quantity_required: float


class MaterialRequestCreate(BaseModel):
    quantity: float


class MaterialRequestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    module_material_id: int
    warehouse_material_id: int
    quantity: float
    status: MaterialRequestStatus
    requested_by_id: int
    decided_by_id: int | None
    created_at: datetime
    decided_at: datetime | None


class ModuleMaterialOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    module_id: int
    warehouse_material_id: int
    inventory_number: str
    unit: str
    quantity_required: float
    quantity_requested: float
    quantity_provided: float
    requests: list[MaterialRequestOut] = []


class ModuleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    production_id: int
    name: str
    description: str | None
    materials: list[ModuleMaterialOut] = []


class ProductionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    cycle_id: int
    house_index: int
    name: str
    created_at: datetime
    modules: list[ModuleOut] = []


class ProductionListOut(BaseModel):
    id: int
    cycle_id: int
    house_index: int
    name: str
    cycle_status: CycleStatus
    created_at: datetime
    module_count: int
