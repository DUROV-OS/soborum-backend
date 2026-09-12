from sqlalchemy.orm import Session

from app.house_models.models import HouseModelCard, HouseModelKind
from app.house_models.schemas import (
    HouseModelBriefOut,
    HouseModelCatalogOut,
    HouseModelSeriesGroupOut,
)

_SERIES_ORDER = ["barn", "flat"]


def _area_sort_key(card: HouseModelCard) -> float:
    # Модели без задокументированной площади застройки (например Barn_DH83 —
    # в буклете нет точной цифры) идут в конец группы, а не путаются с самой
    # маленькой моделью серии.
    return card.area_footprint_m2 if card.area_footprint_m2 is not None else float("inf")


def get_catalog(db: Session) -> HouseModelCatalogOut:
    cards = db.query(HouseModelCard).all()

    catalog_by_series: dict[str, list[HouseModelCard]] = {}
    individual: list[HouseModelCard] = []
    for card in cards:
        if card.kind == HouseModelKind.CATALOG and card.series:
            catalog_by_series.setdefault(card.series, []).append(card)
        else:
            individual.append(card)

    known_series = [s for s in _SERIES_ORDER if s in catalog_by_series]
    other_series = sorted(s for s in catalog_by_series if s not in _SERIES_ORDER)

    series_groups = [
        HouseModelSeriesGroupOut(
            series=series,
            models=[
                HouseModelBriefOut.model_validate(m)
                for m in sorted(catalog_by_series[series], key=_area_sort_key)
            ],
        )
        for series in known_series + other_series
    ]

    individual.sort(key=lambda c: c.title)

    return HouseModelCatalogOut(
        series=series_groups,
        individual=[HouseModelBriefOut.model_validate(m) for m in individual],
    )


def get_by_key(db: Session, key: str) -> HouseModelCard | None:
    return db.query(HouseModelCard).filter(HouseModelCard.key == key).first()
