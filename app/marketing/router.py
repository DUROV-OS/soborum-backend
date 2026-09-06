from datetime import date

from fastapi import Depends, FastAPI
from sqlalchemy.orm import Session

from app.common.module_access import Module as AccessModule
from app.core.deps import require_module
from app.db.session import get_db
from app.marketing import service as marketing_service
from app.marketing import trends_client
from app.marketing.schemas import (
    ContentAnalysisUpdate,
    ContentFinalUpdate,
    ContentItemCreate,
    ContentItemOut,
    ContentItemUpdate,
    ContentRawUpdate,
    PostLinkIn,
)
from app.marketing.trends_schemas import (
    InterestByRegionOut,
    InterestOverTimeOut,
    LookupEntry,
    RelatedOut,
    TrendingNowOut,
)
from app.users.models import User

app = FastAPI(
    title="Soborbum — Маркетинг",
    description="Календарь выпуска контента: от идеи до анализа результатов.",
    version="0.2.1",
)

require_marketing = require_module(AccessModule.MARKETING)


@app.get("/calendar", response_model=list[ContentItemOut])
def calendar(
    db: Session = Depends(get_db),
    _: User = Depends(require_marketing),
    date_from: date | None = None,
    date_to: date | None = None,
):
    items = marketing_service.get_calendar(db, date_from, date_to)
    return [ContentItemOut.from_model(i) for i in items]


@app.post("/content", response_model=ContentItemOut, status_code=201)
def create_content(payload: ContentItemCreate, db: Session = Depends(get_db), _: User = Depends(require_marketing)):
    content = marketing_service.create_content(db, payload)
    db.commit()
    db.refresh(content)
    return ContentItemOut.from_model(content)


@app.get("/content/{content_id}", response_model=ContentItemOut)
def get_content(content_id: int, db: Session = Depends(get_db), _: User = Depends(require_marketing)):
    content = marketing_service.get_content_or_404(db, content_id)
    return ContentItemOut.from_model(content)


@app.patch("/content/{content_id}", response_model=ContentItemOut)
def update_content(
    content_id: int,
    payload: ContentItemUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_marketing),
):
    content = marketing_service.get_content_or_404(db, content_id)
    content = marketing_service.update_basic(db, content, payload)
    db.commit()
    db.refresh(content)
    return ContentItemOut.from_model(content)


@app.patch("/content/{content_id}/raw", response_model=ContentItemOut)
def update_raw(
    content_id: int,
    payload: ContentRawUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_marketing),
):
    content = marketing_service.get_content_or_404(db, content_id)
    content = marketing_service.update_raw(db, content, payload)
    db.commit()
    db.refresh(content)
    return ContentItemOut.from_model(content)


@app.patch("/content/{content_id}/final", response_model=ContentItemOut)
def update_final(
    content_id: int,
    payload: ContentFinalUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_marketing),
):
    content = marketing_service.get_content_or_404(db, content_id)
    content = marketing_service.update_final(db, content, payload)
    db.commit()
    db.refresh(content)
    return ContentItemOut.from_model(content)


@app.put("/content/{content_id}/post-links", response_model=ContentItemOut)
def set_post_links(
    content_id: int,
    payload: list[PostLinkIn],
    db: Session = Depends(get_db),
    _: User = Depends(require_marketing),
):
    content = marketing_service.get_content_or_404(db, content_id)
    content = marketing_service.set_post_links(db, content, payload)
    db.commit()
    db.refresh(content)
    return ContentItemOut.from_model(content)


@app.patch("/content/{content_id}/analysis", response_model=ContentItemOut)
def update_analysis(
    content_id: int,
    payload: ContentAnalysisUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_marketing),
):
    content = marketing_service.get_content_or_404(db, content_id)
    content = marketing_service.update_analysis(db, content, payload)
    db.commit()
    db.refresh(content)
    return ContentItemOut.from_model(content)


@app.post("/content/{content_id}/transition", response_model=ContentItemOut)
def transition_content(content_id: int, db: Session = Depends(get_db), _: User = Depends(require_marketing)):
    content = marketing_service.get_content_or_404(db, content_id)
    content = marketing_service.transition_stage(db, content)
    db.commit()
    db.refresh(content)
    return ContentItemOut.from_model(content)


# --------------------------------------------------------------------------
# Тренд: графики популярности запросов как в Google Trends.
# Источник данных — библиотека trendspy (неофициальный клиент Google Trends,
# замена pytrends). Всё read-only, БД не трогаем.
# --------------------------------------------------------------------------


@app.get("/trends/interest-over-time", response_model=InterestOverTimeOut)
def trends_interest_over_time(
    q: str,
    timeframe: str | None = None,
    geo: str | None = None,
    cat: str | None = None,
    _: User = Depends(require_marketing),
):
    """Динамика популярности во времени — данные для линейного графика.

    ``q`` — один или до пяти запросов через запятую (сравнение).
    ``timeframe`` — окно Google Trends (``now 7-d``, ``today 12-m``,
    ``2024-01-01 2024-12-31``, ``all``), по умолчанию ``today 12-m``.
    ``geo`` — код региона (``US``, ``RU``, ``US-NY``), пусто = весь мир.
    ``cat`` — id категории Google Trends.
    """
    return trends_client.interest_over_time(q, timeframe, geo, cat)


@app.get("/trends/interest-by-region", response_model=InterestByRegionOut)
def trends_interest_by_region(
    q: str,
    timeframe: str | None = None,
    geo: str | None = None,
    resolution: str | None = None,
    _: User = Depends(require_marketing),
):
    """Распределение интереса по регионам — данные для карты/столбчатой диаграммы.

    ``resolution`` — ``COUNTRY`` | ``REGION`` | ``CITY`` | ``DMA``.
    """
    return trends_client.interest_by_region(q, timeframe, geo, resolution)


@app.get("/trends/related-queries", response_model=RelatedOut)
def trends_related_queries(
    q: str,
    timeframe: str | None = None,
    geo: str | None = None,
    _: User = Depends(require_marketing),
):
    """Похожие запросы: ``top`` — самые популярные, ``rising`` — набирающие."""
    return trends_client.related_queries(q, timeframe, geo)


@app.get("/trends/related-topics", response_model=RelatedOut)
def trends_related_topics(
    q: str,
    timeframe: str | None = None,
    geo: str | None = None,
    _: User = Depends(require_marketing),
):
    """Похожие темы: ``top`` и ``rising``."""
    return trends_client.related_topics(q, timeframe, geo)


@app.get("/trends/trending-now", response_model=TrendingNowOut)
def trends_trending_now(
    geo: str | None = None,
    limit: int = 20,
    with_news: bool = False,
    _: User = Depends(require_marketing),
):
    """Что в тренде прямо сейчас в регионе ``geo`` (по умолчанию ``US``).

    ``with_news=true`` — добавляет к каждому тренду связанные новости.
    """
    return trends_client.trending_now(geo, max(1, min(limit, 100)), with_news)


@app.get("/trends/geo", response_model=list[LookupEntry])
def trends_geo(find: str | None = None, _: User = Depends(require_marketing)):
    """Справочник регионов Google Trends (для выбора ``geo``)."""
    return trends_client.geo_lookup(find)


@app.get("/trends/categories", response_model=list[LookupEntry])
def trends_categories(find: str | None = None, _: User = Depends(require_marketing)):
    """Справочник категорий Google Trends (для выбора ``cat``)."""
    return trends_client.categories_lookup(find)
