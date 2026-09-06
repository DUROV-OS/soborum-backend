"""Схемы ответов раздела «Маркетинг → Тренд» (данные Google Trends через trendspy)."""

from pydantic import BaseModel


class InterestPoint(BaseModel):
    date: str | None
    values: dict[str, int | None]
    is_partial: bool = False


class InterestOverTimeOut(BaseModel):
    keywords: list[str]
    timeframe: str
    geo: str
    category: str
    series: list[InterestPoint]


class RegionInterest(BaseModel):
    geo_name: str | None
    geo_code: str | None = None
    value: int | None


class InterestByRegionOut(BaseModel):
    keyword: str
    geo: str
    resolution: str
    regions: list[RegionInterest]


class RelatedEntry(BaseModel):
    query: str | None
    type: str | None = None
    value: int | None = None
    link: str | None = None


class RelatedOut(BaseModel):
    keyword: str
    top: list[RelatedEntry]
    rising: list[RelatedEntry]


class TrendNews(BaseModel):
    title: str | None = None
    url: str | None = None
    source: str | None = None
    picture: str | None = None
    time: str | None = None


class TrendingItem(BaseModel):
    keyword: str
    volume: int | None = None
    volume_growth_pct: float | None = None
    geo: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    trend_keywords: list[str] = []
    topics: list[str] = []
    news: list[TrendNews] = []


class TrendingNowOut(BaseModel):
    geo: str
    with_news: bool
    items: list[TrendingItem]


class LookupEntry(BaseModel):
    name: str | None
    id: str | None
