from pydantic import BaseModel, ConfigDict

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
