from pydantic import BaseModel, ConfigDict

from app.common.files import FileAssetOut
from app.house_models.models import HouseModelConfirmation, HouseModelKind


class HouseModelBriefOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    key: str
    title: str
    kind: HouseModelKind
    series: str | None
    area_footprint_m2: float | None
    area_total_m2: float | None
    price_site_rub: int | None
    deal_amount_rub: int | None
    client_name: str | None
    confirmation: HouseModelConfirmation
    confirmation_label: str


class HouseModelSeriesGroupOut(BaseModel):
    series: str
    models: list[HouseModelBriefOut]


class HouseModelCatalogOut(BaseModel):
    series: list[HouseModelSeriesGroupOut]
    individual: list[HouseModelBriefOut]


class HouseModelDetailOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    key: str
    title: str
    kind: HouseModelKind
    series: str | None
    area_footprint_m2: float | None
    area_total_m2: float | None
    price_site_rub: int | None
    deal_amount_rub: int | None
    client_name: str | None
    confirmation: HouseModelConfirmation
    confirmation_label: str
    source_note_path: str
    planning_image_id: int | None

    characteristics_md: str | None
    planning_md: str | None
    configurations_md: str | None
    modules_md: str | None
    economics_md: str | None
    production_experience_md: str | None
    deals_without_pz_md: str | None
    files_md: str | None
    open_questions_md: str | None
    notes_md: str | None

    # Типовые АР/КР (0073-b) — единственные поля карточки, редактируемые
    # через API (PATCH /catalog/{key}/typical-documents, только админ).
    typical_ar: FileAssetOut | None
    typical_kr: FileAssetOut | None


class HouseModelTypicalDocumentsPatch(BaseModel):
    """Тело PATCH /catalog/{key}/typical-documents. Поле, не переданное в
    запросе, остаётся как было; переданное (в т.ч. `null`) — обновляется,
    очищая ссылку. `exclude_unset=True` на роутере отличает «не пришло» от
    «пришёл null»."""

    typical_ar_file_id: int | None = None
    typical_kr_file_id: int | None = None


class HouseModelProductionOut(BaseModel):
    """Строка `GET /catalog/{key}/productions` — реальные дома этой модели.
    Как и на «Главной» одного производства (`app.production.home`), цена и
    контакты клиента не отдаются: только то, что уже видно в разделе
    «Производство» (номер заказа, имя проекта дома)."""

    production_id: int
    cycle_id: int
    house_index: int
    client_display_name: str
