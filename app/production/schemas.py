from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.common.files import FileAssetOut
from app.cycle.models import CycleStatus
from app.dashboard.schemas import WidgetTone
from app.house_models.schemas import HouseModelBriefOut
from app.production.models import MaterialRequestStatus


class BlockCreate(BaseModel):
    name: str
    description: str | None = None
    sequence: int | None = None
    requires_materials: bool = True


class BlockUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    sequence: int | None = None
    requires_materials: bool | None = None


class BlockDependencyCreate(BaseModel):
    depends_on_id: int


class BlockMaterialCreate(BaseModel):
    warehouse_material_id: int
    inventory_number: str
    unit: str
    quantity_required: float


class BlockMaterialUpdate(BaseModel):
    quantity_required: float


class MaterialRequestCreate(BaseModel):
    quantity: float


class MaterialRequestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    block_material_id: int
    warehouse_material_id: int
    quantity: float
    status: MaterialRequestStatus
    requested_by_id: int
    decided_by_id: int | None
    created_at: datetime
    decided_at: datetime | None


class BlockMaterialOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    block_id: int
    warehouse_material_id: int
    inventory_number: str
    unit: str
    quantity_required: float
    quantity_requested: float
    quantity_provided: float
    requests: list[MaterialRequestOut] = []


class BlockOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    production_id: int
    name: str
    description: str | None
    sequence: int
    requires_materials: bool = True
    depends_on_ids: list[int] = []
    materials: list[BlockMaterialOut] = []


class ProductionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    cycle_id: int
    house_index: int
    name: str
    created_at: datetime
    blocks: list[BlockOut] = []


class ProductionListOut(BaseModel):
    id: int
    cycle_id: int
    house_index: int
    name: str
    cycle_status: CycleStatus
    created_at: datetime
    block_count: int
    is_completed: bool
    criticality: Literal["normal", "warning", "critical"]


# ------------------------------------------------ оценка готовности (0084-b) --
# Поля один в один из app/production/readiness.py; фронт ничего не вычисляет
# сам, только показывает их.

ReadinessMaterialsState = Literal[
    "insufficient_data", "needs_reconciliation", "shortfall", "provided", "not_required"
]


class ReadinessReasonOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    code: str
    text: str
    block_id: int | None = None
    material_id: int | None = None
    task_id: int | None = None


class ReadinessSourcesOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    block_ids: list[int] = []
    material_ids: list[int] = []
    task_ids: list[int] = []
    material_request_ids: list[int] = []


class ReadinessWaitingOnOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    block_id: int
    name: str


class BlockReadinessOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    block_id: int
    production_id: int
    name: str
    materials_state: ReadinessMaterialsState
    materials_label: str
    admitted: bool
    waiting_on: list[ReadinessWaitingOnOut] = []
    reasons: list[ReadinessReasonOut] = []
    sources: ReadinessSourcesOut
    computed_at: datetime
    facts_at: datetime | None
    version: str


class ProductionReadinessOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    production_id: int
    materials_state: ReadinessMaterialsState
    materials_label: str
    reasons: list[ReadinessReasonOut] = []
    sources: ReadinessSourcesOut
    computed_at: datetime
    facts_at: datetime | None
    version: str
    blocks: list[BlockReadinessOut] = []


class ProductionReadinessListItemOut(BaseModel):
    production_id: int
    materials_state: ReadinessMaterialsState
    materials_label: str
    # Только причины, требующие действия (от блоков в проблемных состояниях);
    # пояснения к «обеспечены» / «не требуются» сюда не входят.
    reasons_count: int
    computed_at: datetime
    facts_at: datetime | None
    version: str


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
    # "ai" | "fallback" | "none" (сигналов нет — по графику) |
    # "insufficient_data" (прогноз не построен: не хватает данных, 0084-c)
    source: str
    # Когда посчитан ответ: у закешированного ИИ-ответа — время генерации.
    # None — запись кэша, сохранённая до 0084-c.
    generated_at: datetime | None = None


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


# ------------------------------------------------ шаблон графа этапов (0066-d) --


class KrPageRefOut(BaseModel):
    page_number: int
    note: str | None = None


class TemplateBlockTaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    description: str | None
    kr_page_ref: KrPageRefOut | None


class TemplateBlockMaterialOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    unit: str
    kr_page_ref: KrPageRefOut | None
    warehouse_material_id: int | None
    warehouse_material_title: str | None = None
    confidence: str | None = None


class TemplateBlockOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None
    sequence: int
    requires_materials: bool = True
    depends_on_ids: list[int] = []
    kr_page_refs: list[KrPageRefOut] = []
    tasks: list[TemplateBlockTaskOut] = []
    materials: list[TemplateBlockMaterialOut] = []


class ProductionStageTemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    house_model_key: str | None
    status: str
    source_client_id: int
    created_at: datetime
    confirmed_at: datetime | None
    confirmed_by_id: int | None
    blocks: list[TemplateBlockOut] = []


class TemplateBlockPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    requires_materials: bool | None = None


class TemplateBlockTaskPatch(BaseModel):
    title: str | None = None
    description: str | None = None


class TemplateBlockMaterialPatch(BaseModel):
    name: str | None = None
    unit: str | None = None
    warehouse_material_id: int | None = None
