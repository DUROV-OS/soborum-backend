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


class LookupEntry(BaseModel):
    name: str | None
    id: str | None


# --- «Тренды ниши»: модульные дома / турбазы / строительство ------------------


class KeywordGroup(BaseModel):
    group: str
    keywords: list[str]


class NicheKeywordsOut(BaseModel):
    geo: str
    groups: list[KeywordGroup]


class NicheTopicStat(BaseModel):
    keyword: str
    group: str
    current: int | None
    average: int | None
    peak: int | None
    peak_date: str | None
    growth_pct: float | None
    direction: str  # rising | flat | falling | n/a


class NicheOverviewOut(BaseModel):
    timeframe: str
    geo: str
    resolved: int  # сколько запросов реально отдал Google
    unavailable: list[str]
    topics: list[NicheTopicStat]


class NicheRegion(BaseModel):
    geo_name: str | None
    geo_code: str | None = None
    value: int | None  # 0..100, усреднено по ключевым запросам ниши


class NicheRegionsOut(BaseModel):
    keywords: list[str]
    timeframe: str
    geo: str
    resolution: str
    regions: list[NicheRegion]


class NicheRisingEntry(BaseModel):
    query: str
    value: int | None
    seed: str  # какой запрос ниши вывел эту тему


class NicheRisingOut(BaseModel):
    timeframe: str
    geo: str
    seeds_used: list[str]
    unavailable: list[str]
    rising: list[NicheRisingEntry]
