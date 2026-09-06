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
import time
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Callable

from fastapi import HTTPException, status

DEFAULT_TIMEFRAME = "today 12-m"

# «Человеческий» referer заметно снижает частоту отказов Google по квоте
# (совет самой trendspy для related_*). Для остальных методов безвреден.
_GT_HEADERS = {"referer": "https://www.google.com/"}

# Google Trends жёстко лимитирует неофициальный доступ, а «Тренды ниши»
# бьют по нему десятками запросов. Данные меняются медленно (недельный шаг),
# поэтому держим короткий кэш ответов в памяти процесса.
_CACHE_TTL_SECONDS = 1800.0
_cache: dict[tuple, tuple[float, Any]] = {}


def _cached(key: tuple, producer: Callable[[], Any]) -> Any:
    now = time.monotonic()
    hit = _cache.get(key)
    if hit is not None and now - hit[0] < _CACHE_TTL_SECONDS:
        return hit[1]
    value = producer()  # ошибки Google (HTTPException) наверх, в кэш не кладём
    _cache[key] = (now, value)
    return value


# Ключевые запросы бизнеса «модульные дома»: сгруппированы для фильтров на фронте.
# Google Trends понимает русские запросы; порядок групп = порядок вывода.
NICHE_KEYWORDS: dict[str, list[str]] = {
    "Модульные дома": [
        "модульный дом",
        "модульные дома",
        "дом под ключ",
        "каркасный дом",
        "дом из сип панелей",
        "барнхаус",
        "дом а-фрейм",
        "префаб дом",
    ],
    "Турбазы и глэмпинг": [
        "глэмпинг",
        "модульная база отдыха",
        "турбаза",
        "модульный отель",
        "дом для глэмпинга",
        "база отдыха под ключ",
    ],
    "Тренды строительства": [
        "модульное строительство",
        "быстровозводимые дома",
        "каркасное строительство",
        "дом из бруса",
        "строительство дома 2026",
    ],
    "Бани и хозпостройки": [
        "модульная баня",
        "баня бочка",
        "хозблок",
        "дачный домик",
    ],
}

# Чем меряем «в каких регионах интересуются» и «что растёт», если q не задан.
NICHE_CORE_KEYWORDS = ["модульный дом", "каркасный дом", "глэмпинг"]


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


def _cache_key(method: str, *args, **kwargs) -> tuple:
    # dict-аргументы (headers) в ключ не идут — на результат они не влияют
    return (
        method,
        tuple(tuple(a) if isinstance(a, list) else a for a in args),
        tuple(sorted((k, v) for k, v in kwargs.items() if not isinstance(v, dict))),
    )


def _is_fresh(key: tuple) -> bool:
    hit = _cache.get(key)
    return hit is not None and time.monotonic() - hit[0] < _CACHE_TTL_SECONDS


def _call_cached(method: str, *args, **kwargs) -> Any:
    """``_call`` с кэшем ответов в памяти процесса (TTL ~30 мин)."""
    return _cached(_cache_key(method, *args, **kwargs), lambda: _call(method, *args, **kwargs))


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
    df = _call_cached(
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
    df = _call_cached(
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
    result = _call_cached(
        "related_queries",
        keyword,
        timeframe=timeframe or DEFAULT_TIMEFRAME,
        geo=geo or "",
        headers=_GT_HEADERS,
    )
    return {"keyword": keyword, **_split_top_rising(result, keyword)}


def related_topics(q: str, timeframe: str | None, geo: str | None) -> dict:
    keyword = _keywords(q)[0]
    result = _call_cached(
        "related_topics",
        keyword,
        timeframe=timeframe or DEFAULT_TIMEFRAME,
        geo=geo or "",
        headers=_GT_HEADERS,
    )
    return {"keyword": keyword, **_split_top_rising(result, keyword)}


# ------------------------------------------------------ «Тренды ниши» ---------
# Вместо общих «горячих запросов Google» — срез по бизнесу «модульные дома»:
# что растёт/падает в спросе, в каких регионах РФ интересуются, какие темы
# набирают. Всё собирается из тех же нормализованных функций выше; каждый
# внешний вызов может упасть по лимиту Google — такие запросы уходят в
# ``unavailable``, а не роняют весь ответ.


def _select_groups(groups: str | None) -> dict[str, list[str]]:
    if not groups:
        return NICHE_KEYWORDS
    wanted = {g.strip() for g in groups.split(",") if g.strip()}
    picked = {name: kws for name, kws in NICHE_KEYWORDS.items() if name in wanted}
    return picked or NICHE_KEYWORDS


def _chunks(seq: list[str], size: int = 5) -> list[list[str]]:
    return [seq[i : i + size] for i in range(0, len(seq), size)]


def _topic_stat(series: list[dict], kw: str) -> dict | None:
    points = [(p["date"], p["values"].get(kw)) for p in series]
    nums = [(d, v) for d, v in points if v is not None]
    if not nums:
        return None
    values = [v for _, v in nums]
    peak = max(values)
    window = max(1, min(4, len(values) // 3))
    early = sum(values[:window]) / window
    late = sum(values[-window:]) / window
    growth = round(((late - early) / early) * 100, 1) if early > 0 else None
    if growth is None:
        direction = "n/a"
    elif growth >= 15:
        direction = "rising"
    elif growth <= -15:
        direction = "falling"
    else:
        direction = "flat"
    return {
        "current": values[-1],
        "average": round(sum(values) / len(values)),
        "peak": peak,
        "peak_date": next(d for d, v in nums if v == peak),
        "growth_pct": growth,
        "direction": direction,
    }


def niche_overview(timeframe: str | None, geo: str | None, groups: str | None) -> dict:
    timeframe = timeframe or DEFAULT_TIMEFRAME
    geo = "RU" if geo is None else geo
    topics: list[dict] = []
    unavailable: list[str] = []
    for group, keywords in _select_groups(groups).items():
        for chunk in _chunks(keywords, 5):
            try:
                data = interest_over_time(",".join(chunk), timeframe, geo, None)
            except HTTPException:
                unavailable.extend(chunk)
                continue
            for kw in chunk:
                stat = _topic_stat(data["series"], kw)
                if stat is None:
                    unavailable.append(kw)
                    continue
                topics.append({"keyword": kw, "group": group, **stat})
    topics.sort(key=lambda t: (t["growth_pct"] is None, -(t["growth_pct"] or -1e9)))
    return {
        "timeframe": timeframe,
        "geo": geo,
        "resolved": len(topics),
        "unavailable": unavailable,
        "topics": topics,
    }


def niche_regions(q: str | None, timeframe: str | None, geo: str | None, resolution: str | None) -> dict:
    timeframe = timeframe or DEFAULT_TIMEFRAME
    geo = "RU" if geo is None else geo
    resolution = (resolution or "REGION").upper()
    keywords = _keywords(q) if q else list(NICHE_CORE_KEYWORDS)
    acc: dict[str, dict] = {}
    used: list[str] = []
    for kw in keywords[:5]:
        try:
            data = interest_by_region(kw, timeframe, geo, resolution)
        except HTTPException:
            continue
        used.append(kw)
        for region in data["regions"]:
            name = region["geo_name"]
            if not name:
                continue
            slot = acc.setdefault(name, {"geo_code": region["geo_code"], "sum": 0, "n": 0})
            if region["value"] is not None:
                slot["sum"] += region["value"]
                slot["n"] += 1
    regions = [
        {
            "geo_name": name,
            "geo_code": slot["geo_code"],
            "value": round(slot["sum"] / slot["n"]) if slot["n"] else None,
        }
        for name, slot in acc.items()
    ]
    regions.sort(key=lambda r: (r["value"] is None, -(r["value"] or 0)))
    return {
        "keywords": used,
        "timeframe": timeframe,
        "geo": geo,
        "resolution": resolution,
        "regions": regions,
    }


def niche_rising(timeframe: str | None, geo: str | None, limit: int, groups: str | None) -> dict:
    timeframe = timeframe or DEFAULT_TIMEFRAME
    geo = "RU" if geo is None else geo
    seeds = [kw for keywords in _select_groups(groups).values() for kw in keywords][:5]
    merged: dict[str, dict] = {}
    used: list[str] = []
    unavailable: list[str] = []
    for seed in seeds:
        cached = _is_fresh(_cache_key("related_queries", seed, timeframe=timeframe, geo=geo or ""))
        if used and not cached:
            # related_queries у Google лимитируется жёстче всего — разносим живые вызовы
            time.sleep(0.6)
        try:
            data = related_queries(seed, timeframe, geo)
        except HTTPException:
            unavailable.append(seed)
            continue
        used.append(seed)
        pool = data["rising"] or data["top"]
        for entry in pool:
            query = (entry.get("query") or "").strip()
            if not query:
                continue
            value = entry.get("value")
            current = merged.get(query)
            if current is None or (value or 0) > (current["value"] or 0):
                merged[query] = {"query": query, "value": value, "seed": seed}
    rising = sorted(merged.values(), key=lambda e: -(e["value"] or 0))[: max(1, limit)]
    return {
        "timeframe": timeframe,
        "geo": geo,
        "seeds_used": used,
        "unavailable": unavailable,
        "rising": rising,
    }


def niche_keywords(geo: str | None) -> dict:
    return {
        "geo": "RU" if geo is None else geo,
        "groups": [{"group": name, "keywords": list(kws)} for name, kws in NICHE_KEYWORDS.items()],
    }


def geo_lookup(find: str | None) -> list[dict]:
    raw = _call("geo", find=find) if find else _call("geo")
    return [{"name": r.get("name"), "id": str(r.get("id"))} for r in (raw or []) if isinstance(r, dict)]


def categories_lookup(find: str | None) -> list[dict]:
    raw = _call("categories", find=find) if find else _call("categories")
    return [{"name": r.get("name"), "id": str(r.get("id"))} for r in (raw or []) if isinstance(r, dict)]
