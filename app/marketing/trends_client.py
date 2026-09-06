"""Тонкая обёртка над библиотекой ``trendspy`` (неофициальный клиент Google
Trends, замена pytrends). Здесь только получение данных и приведение ответов
``trendspy`` (pandas.DataFrame / объекты) к простым python-структурам, которые
без потерь сериализуются в JSON и рисуются на фронте как графики Google Trends.

Импорт ``trendspy`` ленивый: если пакет не установлен или Google вернул
ошибку/капчу/лимит, эндпоинты отдают 503/502, а остальное приложение
продолжает работать.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

from fastapi import HTTPException, status

DEFAULT_TIMEFRAME = "today 12-m"

# «Человеческий» referer заметно снижает частоту отказов Google по квоте
# (совет самой trendspy для related_*). Для остальных методов безвреден.
_GT_HEADERS = {"referer": "https://www.google.com/"}


@lru_cache(maxsize=1)
def _trends():
    try:
        from trendspy import Trends  # noqa: PLC0415  (ленивый импорт по задумке)
    except ImportError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Библиотека trendspy не установлена на сервере",
        ) from exc
    return Trends()


def _call(method: str, *args, **kwargs) -> Any:
    client = _trends()
    try:
        return getattr(client, method)(*args, **kwargs)
    except HTTPException:
        raise
    except TypeError:
        # старая сигнатура trendspy без части kwargs — повторяем без headers
        kwargs.pop("headers", None)
        try:
            return getattr(client, method)(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Google Trends недоступен: {exc}") from exc
    except Exception as exc:  # сеть, капча, лимит квоты, смена вёрстки Google
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Google Trends недоступен: {exc}",
        ) from exc


# --------------------------------------------------------------------- utils --

def _keywords(q: str) -> list[str]:
    items = [part.strip() for part in q.split(",") if part.strip()]
    if not items:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Не задан поисковый запрос")
    if len(items) > 5:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Google Trends сравнивает не больше 5 запросов за раз",
        )
    return items


def _int(value: Any) -> int | None:
    try:
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return None
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _cat(cat: str | None) -> int:
    try:
        return int(cat) if cat else 0
    except (TypeError, ValueError):
        return 0


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (tuple, list)):
        value = value[0] if value else None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _records(df) -> list[dict]:
    """DataFrame (или None/пустой) -> список dict по строкам."""
    if df is None or getattr(df, "empty", True):
        return []
    return df.reset_index().to_dict(orient="records")


# ---------------------------------------------------------------- normalizers --

def interest_over_time(q: str, timeframe: str | None, geo: str | None, cat: str | None) -> dict:
    keywords = _keywords(q)
    timeframe = timeframe or DEFAULT_TIMEFRAME
    df = _call(
        "interest_over_time",
        keywords,
        timeframe=timeframe,
        geo=geo or "",
        cat=_cat(cat),
        headers=_GT_HEADERS,
    )
    series: list[dict] = []
    if df is not None and not getattr(df, "empty", True):
        columns = list(df.columns)
        for idx, row in df.iterrows():
            series.append(
                {
                    "date": _iso(idx),
                    "values": {kw: _int(row.get(kw)) for kw in keywords if kw in columns},
                    "is_partial": bool(row.get("isPartial", False)),
                }
            )
    return {
        "keywords": keywords,
        "timeframe": timeframe,
        "geo": geo or "",
        "category": str(_cat(cat)),
        "series": series,
    }


def interest_by_region(q: str, timeframe: str | None, geo: str | None, resolution: str | None) -> dict:
    keyword = _keywords(q)[0]
    resolution = (resolution or "COUNTRY").upper()
    df = _call(
        "interest_by_region",
        keyword,
        timeframe=timeframe or DEFAULT_TIMEFRAME,
        geo=geo or "",
        resolution=resolution,
    )
    regions: list[dict] = []
    if df is not None and not getattr(df, "empty", True):
        columns = list(df.columns)
        for idx, row in df.iterrows():
            name = row.get("geoName") if "geoName" in columns else idx
            value = row.get(keyword)
            if value is None:
                value = next(
                    (row.get(c) for c in columns if c not in ("geoName", "geoCode")),
                    None,
                )
            regions.append(
                {
                    "geo_name": str(name) if name is not None else None,
                    "geo_code": row.get("geoCode") if "geoCode" in columns else None,
                    "value": _int(value),
                }
            )
    regions.sort(key=lambda r: (r["value"] is None, -(r["value"] or 0)))
    return {"keyword": keyword, "geo": geo or "", "resolution": resolution, "regions": regions}


def _related_block(df) -> list[dict]:
    out: list[dict] = []
    for rec in _records(df):
        out.append(
            {
                "query": rec.get("query") or rec.get("topic_title") or rec.get("title"),
                "type": rec.get("topic_type"),
                "value": _int(rec.get("value")),
                "link": rec.get("link"),
            }
        )
    return out


def _split_top_rising(result: Any, keyword: str) -> dict:
    # trendspy: dict {'top': df, 'rising': df}; при нескольких словах — dict по слову.
    if isinstance(result, dict):
        inner = result.get(keyword, result)
        if isinstance(inner, dict):
            return {"top": _related_block(inner.get("top")), "rising": _related_block(inner.get("rising"))}
    return {"top": _related_block(result), "rising": []}


def related_queries(q: str, timeframe: str | None, geo: str | None) -> dict:
    keyword = _keywords(q)[0]
    result = _call(
        "related_queries",
        keyword,
        timeframe=timeframe or DEFAULT_TIMEFRAME,
        geo=geo or "",
        headers=_GT_HEADERS,
    )
    return {"keyword": keyword, **_split_top_rising(result, keyword)}


def related_topics(q: str, timeframe: str | None, geo: str | None) -> dict:
    keyword = _keywords(q)[0]
    result = _call(
        "related_topics",
        keyword,
        timeframe=timeframe or DEFAULT_TIMEFRAME,
        geo=geo or "",
        headers=_GT_HEADERS,
    )
    return {"keyword": keyword, **_split_top_rising(result, keyword)}


def _news(item: Any) -> dict:
    def field(name: str) -> Any:
        return item.get(name) if isinstance(item, dict) else getattr(item, name, None)

    return {
        "title": field("title"),
        "url": field("url"),
        "source": field("source"),
        "picture": field("picture"),
        "time": _iso(field("time")),
    }


def trending_now(geo: str | None, limit: int, with_news: bool) -> dict:
    geo = geo or "US"
    raw = _call("trending_now_by_rss", geo=geo) if with_news else _call("trending_now", geo=geo)
    items: list[dict] = []
    for t in list(raw or [])[:limit]:
        if isinstance(t, str):
            items.append({"keyword": t})
            continue
        items.append(
            {
                "keyword": getattr(t, "keyword", None) or str(t),
                "volume": _int(getattr(t, "volume", None)),
                "volume_growth_pct": getattr(t, "volume_growth_pct", None),
                "geo": getattr(t, "geo", None) or geo,
                "started_at": _iso(getattr(t, "started_timestamp", None)),
                "ended_at": _iso(getattr(t, "ended_timestamp", None)),
                "trend_keywords": [str(x) for x in (getattr(t, "trend_keywords", []) or [])],
                "topics": [str(x) for x in (getattr(t, "topics", []) or [])],
                "news": [_news(n) for n in (getattr(t, "news", None) or [])],
            }
        )
    return {"geo": geo, "with_news": with_news, "items": items}


def geo_lookup(find: str | None) -> list[dict]:
    raw = _call("geo", find=find) if find else _call("geo")
    return [{"name": r.get("name"), "id": str(r.get("id"))} for r in (raw or []) if isinstance(r, dict)]


def categories_lookup(find: str | None) -> list[dict]:
    raw = _call("categories", find=find) if find else _call("categories")
    return [{"name": r.get("name"), "id": str(r.get("id"))} for r in (raw or []) if isinstance(r, dict)]
