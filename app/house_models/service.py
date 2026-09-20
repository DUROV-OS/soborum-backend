from sqlalchemy.orm import Session

from app.clients.models import Client
from app.cycle.models import Cycle
from app.house_models.models import HouseModelCard, HouseModelKind
from app.house_models.schemas import (
    HouseModelBriefOut,
    HouseModelCatalogOut,
    HouseModelProductionOut,
    HouseModelSeriesGroupOut,
)
from app.production.models import Production

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


def _production_display_name(production: Production) -> str:
    # Тот же ярлык, что уже показывает карточка производства в разделе
    # «Производство» (frontend ProductionOverviewPage) — номер заказа и, если
    # задано, имя проекта дома. Ничего сверх этого (цена/контакты клиента) не
    # раскрывается — тот же принцип, что на «Главной» одного производства
    # (app.production.home.build_documents).
    label = f"Заказ №{production.cycle_id}"
    if production.name and production.name != "Дом":
        label = f"{label} · {production.name}"
    return label


def get_model_productions(db: Session, key: str) -> list[HouseModelProductionOut]:
    """Реальные дома этой каталожной модели (задача 0073-b): каждый
    `Production`, чей `Cycle.client.house_model_key == key`. Переход к
    задачам — через уже существующий `/production/{id}`, здесь никакой
    отдельный редактор задач не заводится."""
    rows = (
        db.query(Production)
        .join(Cycle, Cycle.id == Production.cycle_id)
        .join(Client, Client.cycle_id == Cycle.id)
        .filter(Client.house_model_key == key)
        .order_by(Production.cycle_id.desc(), Production.house_index.asc())
        .all()
    )
    return [
        HouseModelProductionOut(
            production_id=p.id,
            cycle_id=p.cycle_id,
            house_index=p.house_index,
            client_display_name=_production_display_name(p),
        )
        for p in rows
    ]
