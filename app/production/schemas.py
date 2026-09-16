from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.common.files import FileAssetOut
from app.cycle.models import CycleStatus
from app.dashboard.schemas import WidgetTone
from app.house_models.schemas import HouseModelBriefOut
from app.production.models import MaterialRequestStatus


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


# --------------------------------------------------------------- «Главная» --
# Вкладка «Главная» одного производства (0065-a) — те же виджеты, что на
# «Пульсе» («Требует внимания», «Актуальное»), но пересчитанные по одному
# циклу/дому, плюс урезанный набор документов клиента (без цены/контактов —
# право production не должно их раскрывать, см. router.py).


class ProductionAttentionOut(BaseModel):
    id: str
    title: str
    description: str
    href: str
    tone: WidgetTone = "warning"


class ProductionAktualnoeOut(BaseModel):
    stage: str
    percent: int = Field(ge=0, le=100)
    phrase: str = ""


class ProductionHomeDocumentsOut(BaseModel):
    house_model: HouseModelBriefOut | None
    ar_file: FileAssetOut | None
    kr_file: FileAssetOut | None
    house_project_file: FileAssetOut | None


class DeadlineInsightOut(BaseModel):
    title: str
    description: str
    impact: str
    source: str  # "ai" | "fallback" | "none"


class ProductionHomeOut(BaseModel):
    actions: list[ProductionAttentionOut] = Field(default_factory=list)
    aktualnoe: ProductionAktualnoeOut | None
    deadlines: DeadlineInsightOut
    documents: ProductionHomeDocumentsOut


# --------------------------------------------------------- разбор КР (0066-c) --


class KrPageOut(BaseModel):
    page_number: int
    text: str
    image_file_id: int


class KrExtractionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    client_id: int
    pages: list[KrPageOut]
    extracted_at: datetime
