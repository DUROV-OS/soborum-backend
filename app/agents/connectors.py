"""Read-only amoCRM and МойСклад snapshots for gather().

Live numbers are freshness, weaker than a later vault `kind: fact`.
These clients only GET. A missing token stays an honest stub.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timezone

import httpx

from app.agents.ids import AgentId
from app.agents.types import ContextHit
from app.core.config import settings
from app.core.mcp_remote import McpTarget, as_rows, client_for

log = logging.getLogger("app.agents.connectors")

_CACHE_TTL = 60.0
_cache: dict[str, tuple[float, object]] = {}
_charts_ready: list[dict] = []
_charts_lock = threading.Lock()
_charts_refreshing = False

_CRM_ROLES = {AgentId.COORDINATOR, AgentId.SALES, AgentId.MARKETER, AgentId.FINANCE}
_STOCK_ROLES = {AgentId.COORDINATOR, AgentId.WAREHOUSE, AgentId.PRODUCTION, AgentId.FINANCE}
_WON, _LOST = 142, 143


def clear_cache() -> None:
    _cache.clear()


def live_charts(*, wait: bool = False) -> list[dict]:
    fresh = _cached("charts_ready")
    if isinstance(fresh, list):
        return list(fresh)
    if _charts_ready and not wait:
        _kick_chart_refresh()
        return list(_charts_ready)
    if wait:
        return _build_and_store_charts()
    _kick_chart_refresh()
    return list(_charts_ready)


def live_briefing(*, wait: bool = True) -> dict:
    """Compact live snapshot for Marina consult. Same sources as the shift.

    wait=False: only cached CRM/stock (kick background refresh). Use this in the
    consult system prompt so a cold amoCRM dump cannot block Claude for ~30s.
    """
    leads, stock, orders, tasks = _chart_sources(wait=wait)
    negatives = [row for row in stock if _qty(row) < 0]
    negatives.sort(key=_qty)
    stuck = [lead for lead in leads if (age := _lead_age(lead)) is not None and age >= 30]
    unpriced = [lead for lead in leads if _as_float(lead.get("price")) <= 0]
    priced = [lead for lead in leads if _as_float(lead.get("price")) > 0]
    unpaid = [
        order
        for order in orders
        if _as_float(_order_sum(order)) - _as_float(order.get("payedSum")) > 1
    ]
    aged_tasks = []
    for task in tasks:
        age = _age_days(task.get("moment") or task.get("updated"))
        if age is None:
            continue
        aged_tasks.append({"name": task.get("name"), "age_days": age, "moment": task.get("moment")})
    aged_tasks.sort(key=lambda row: row["age_days"], reverse=True)
    stuck_named = []
    for lead in leads:
        age = _lead_age(lead)
        if age is None or age < 7:
            continue
        stuck_named.append(
            {
                "id": lead.get("id"),
                "name": lead.get("name"),
                "age_days": age,
                "price": _as_float(lead.get("price")),
            }
        )
    stuck_named.sort(key=lambda row: row["age_days"], reverse=True)
    return {
        "crm_connected": _amocrm_configured(),
        "stock_connected": _moysklad_configured(),
        "open_deals": len(leads),
        "unpriced": len(unpriced),
        "priced": len(priced),
        "priced_sum": sum(_as_float(lead.get("price")) for lead in priced),
        "stuck_30d": len(stuck),
        "stuck_deals": stuck_named[:8],
        "priced_deals": [
            {"id": lead.get("id"), "name": lead.get("name"), "price": _as_float(lead.get("price"))}
            for lead in sorted(priced, key=lambda item: _as_float(item.get("price")), reverse=True)[:12]
        ],
        "stock_rows": len(stock),
        "negatives": len(negatives),
        "negative_items": [
            {"name": row.get("name") or row.get("code"), "qty": _qty(row)} for row in negatives[:12]
        ],
        "orders": [
            {
                "name": order.get("name"),
                "sum": _as_float(_order_sum(order)),
                "payed": _as_float(order.get("payedSum")),
                "shipped": _as_float(order.get("shippedSum")),
            }
            for order in orders
        ],
        "unpaid_orders": len(unpaid),
        "tasks": aged_tasks[:8],
        "task_rows": len(tasks),
    }


def live_briefing_text(*, wait: bool = False) -> str:
    data = live_briefing(wait=wait)
    if not data["crm_connected"] and not data["stock_connected"]:
        return (
            "Живой срез: amoCRM и МойСклад не подключены. "
            "Пустой модуль Soborbum «Склад» не значит, что на производстве всё есть. "
            "Не выдумывай остатки и сделки."
        )
    if data["open_deals"] == 0 and data["stock_rows"] == 0 and data["task_rows"] == 0 and not data["orders"]:
        return (
            "Живой срез amoCRM/МойСклад сейчас пуст или ещё собирается. "
            "Не утверждай, что на складе всё в достаточном количестве. "
            "Для точных минусов вызови live_stock_gaps / live_company_pulse."
        )
    worst = ", ".join(
        f"{item['name']} ({item['qty']:g})" for item in data["negative_items"][:6]
    ) or "в этой выборке без минуса"
    tasks = ", ".join(f"{item['name']} {item['age_days']} дн." for item in data["tasks"][:4]) or "нет"
    unpaid_money = sum(max(order["sum"] - order["payed"], 0) for order in data["orders"])
    return (
        "Живой срез компании (amoCRM + МойСклад), тот же что у смены. "
        "Слабее позднего факта в vault. Модуль Soborbum «Склад» — другая пустая база, её не путай с МойСклад.\n"
        f"Сделки: открытых {data['open_deals']}, без цены {data['unpriced']}, "
        f"без движения >30д {data['stuck_30d']}, с суммой {data['priced']} на {_money(data['priced_sum'])}.\n"
        f"Склад: строк {data['stock_rows']}, позиций в минусе {data['negatives']}. Хуже всех: {worst}.\n"
        f"Заказы покупателей: {len(data['orders'])}, без оплаты {data['unpaid_orders']}, "
        f"не оплачено {_money(unpaid_money)}.\n"
        f"Задания цеха: {data['task_rows']}. Старше всех: {tasks}.\n"
        "Пиши по этим числам. Если спрашивают «что требует поставки» — это минусы МойСклад, не пустой Soborbum."
    )


def live_stance_for(agent_id: AgentId) -> str | None:
    lines = [
        chart["lead"]
        for chart in live_charts(wait=True)
        if agent_id.value in chart.get("agents", []) and chart.get("lead")
    ]
    return " ".join(lines[:2]) or None


def _kick_chart_refresh() -> None:
    global _charts_refreshing
    with _charts_lock:
        if _charts_refreshing:
            return
        _charts_refreshing = True
    thread = threading.Thread(target=_refresh_charts_quiet, daemon=True)
    thread.start()


def _refresh_charts_quiet() -> None:
    global _charts_refreshing
    try:
        _build_and_store_charts()
    except Exception as error:
        log.warning("фоновые графики не собрались: %s", error)
    finally:
        with _charts_lock:
            _charts_refreshing = False


def _build_and_store_charts() -> list[dict]:
    global _charts_ready
    leads, stock, orders, tasks = _chart_sources()
    charts = [
        *_coordinator_charts(leads, stock, orders),
        *_sales_charts(leads),
        *_finance_charts(leads, orders),
        *_warehouse_charts(stock),
        *_production_charts(tasks),
    ]
    charts = [chart for chart in charts if chart.get("bars")]
    _charts_ready = charts
    _store("charts_ready", charts)
    return charts


def live_hits(query: str, agents: list[AgentId]) -> list[ContextHit]:
    hits: list[ContextHit] = []
    if any(agent in _CRM_ROLES for agent in agents):
        hits.extend(_amocrm_hits(query))
    if any(agent in _STOCK_ROLES for agent in agents):
        hits.extend(_moysklad_hits(query))
    return hits


def _amocrm_configured() -> bool:
    return settings.amocrm_mcp_configured or bool(
        settings.amocrm_subdomain and settings.amocrm_long_lived_token
    )


def _moysklad_configured() -> bool:
    return settings.moysklad_mcp_configured or bool(settings.moysklad_token)


def _amocrm_target() -> McpTarget:
    return McpTarget(
        name="amocrm",
        url=settings.amocrm_mcp_url,
        client_id=settings.amocrm_mcp_client_id,
        client_secret=settings.amocrm_mcp_client_secret,
        scope=settings.amocrm_mcp_scope,
        redirect_uri=settings.mcp_redirect_uri,
    )


def _moysklad_target() -> McpTarget:
    return McpTarget(
        name="moysklad",
        url=settings.moysklad_mcp_url,
        client_id=settings.moysklad_mcp_client_id,
        client_secret=settings.moysklad_mcp_client_secret,
        scope=settings.moysklad_mcp_scope,
        redirect_uri=settings.mcp_redirect_uri,
    )


def _amocrm_hits(query: str) -> list[ContextHit]:
    if not _amocrm_configured():
        return [_stub("crm", "amoCRM", "amocrm", "MCP amoCRM не задан. Сделки не выдумываем.")]
    try:
        leads, statuses = _amocrm_snapshot()
    except Exception as error:
        log.warning("amoCRM не прочитался: %s", error)
        return [_stub("crm", "amoCRM", "amocrm", f"amoCRM не ответил: {error}. Сделки не выдумываем.")]

    open_leads = _open_leads(leads)
    picked = _pick(query, open_leads, keys=("name", "id"))
    now = _now()
    hits: list[ContextHit] = []
    for lead in picked[:6]:
        status = statuses.get(int(lead.get("status_id") or 0), "открыта")
        price = _money(lead.get("price"))
        name = str(lead.get("name") or f"сделка {lead.get('id')}")
        hits.append(
            ContextHit(
                source="crm",
                title=f"amoCRM: {name}",
                excerpt=(
                    f"{name}, {price}, этап «{status}». "
                    f"Снимок {now}. Слабее позднего факта в 03_Clients."
                ),
                kind="record",
                path=f"amocrm/leads/{lead.get('id')}",
            )
        )
    if not hits:
        hits.append(_stub("crm", "amoCRM", "amocrm/empty", f"Открытых сделок нет. Снимок {now}."))
    return hits


def _moysklad_hits(query: str) -> list[ContextHit]:
    if not _moysklad_configured():
        return [_stub("warehouse", "МойСклад", "moysklad", "MCP МойСклада не задан. Остатки не выдумываем.")]
    try:
        stock, orders = _moysklad_snapshot()
    except Exception as error:
        log.warning("МойСклад не прочитался: %s", error)
        return [_stub("warehouse", "МойСклад", "moysklad", f"МойСклад не ответил: {error}. Остатки не выдумываем.")]

    now = _now()
    hits: list[ContextHit] = []
    low = sorted(stock, key=_qty)
    picked_stock = _pick(query, low, keys=("name", "code", "article")) or low[:6]
    for row in picked_stock[:6]:
        name = str(row.get("name") or row.get("code") or "позиция")
        qty = _qty(row)
        unit = "шт"
        uom = row.get("uom")
        if isinstance(uom, dict):
            unit = str(uom.get("name") or unit)
        elif isinstance(uom, str) and uom:
            unit = uom
        hits.append(
            ContextHit(
                source="warehouse",
                title=f"МойСклад: {name}",
                excerpt=f"{name} — остаток {qty} {unit}. Снимок {now}. Актуально в МойСкладе, слабее позднего факта в базе.",
                kind="record",
                path=f"moysklad/stock/{row.get('code') or name}",
            )
        )
    picked_orders = _pick(query, orders, keys=("name",)) or orders[:3]
    for order in picked_orders[:3]:
        name = str(order.get("name") or "заказ")
        hits.append(
            ContextHit(
                source="warehouse",
                title=f"МойСклад заказ: {name}",
                excerpt=(
                    f"{name}, сумма {_money(_order_sum(order))}, "
                    f"дата {order.get('moment') or '—'}. Снимок {now}."
                ),
                kind="record",
                path=f"moysklad/customerorder/{order.get('id')}",
            )
        )
    if not hits:
        hits.append(_stub("warehouse", "МойСклад", "moysklad/empty", f"Остатков в отчёте нет. Снимок {now}."))
    return hits


def _amocrm_snapshot() -> tuple[list[dict], dict[int, str]]:
    cached = _cached("amocrm")
    if cached is not None:
        return cached  # type: ignore[return-value]
    if settings.amocrm_mcp_configured:
        deals = as_rows(client_for(_amocrm_target()).call_tool("get_all_deals", {}, timeout=45.0))
        payload = (deals, {})
        _store("amocrm", payload)
        return payload
    base = f"https://{settings.amocrm_subdomain}.{settings.amocrm_base_domain}/api/v4"
    headers = {"Authorization": f"Bearer {settings.amocrm_long_lived_token}"}
    leads = _get(f"{base}/leads", headers, {"limit": 50, "order[updated_at]": "desc", "with": "contacts"})
    rows = (leads or {}).get("_embedded", {}).get("leads", []) or []
    pipelines = _get(f"{base}/leads/pipelines", headers, None)
    statuses: dict[int, str] = {}
    for pipeline in (pipelines or {}).get("_embedded", {}).get("pipelines", []) or []:
        for status in (pipeline.get("_embedded") or {}).get("statuses", []) or []:
            statuses[int(status["id"])] = str(status.get("name") or status["id"])
    payload = (rows, statuses)
    _store("amocrm", payload)
    return payload


def _moysklad_snapshot() -> tuple[list[dict], list[dict]]:
    cached = _cached("moysklad")
    if cached is not None:
        return cached  # type: ignore[return-value]
    if settings.moysklad_mcp_configured:
        target = _moysklad_target()
        stock = _mcp_pages(target, "get_stock_all", limit=100, max_rows=2000, timeout=30.0)
        orders = _mcp_pages(target, "list_customer_orders", limit=50, max_rows=200, timeout=30.0)
        payload = (stock, orders)
        _store("moysklad", payload)
        return payload
    headers = {"Authorization": f"Bearer {settings.moysklad_token}", "Accept-Encoding": "gzip"}
    base = "https://api.moysklad.ru/api/remap/1.2"
    stock = _rest_pages(f"{base}/report/stock/all", headers, limit=100, max_rows=2000)
    orders = _rest_pages(f"{base}/entity/customerorder", headers, limit=50, max_rows=200, extra={"order": "moment,desc"})
    payload = (stock, orders)
    _store("moysklad", payload)
    return payload


def _mcp_pages(target: McpTarget, tool: str, *, limit: int, max_rows: int, timeout: float) -> list[dict]:
    remote = client_for(target)
    rows: list[dict] = []
    offset = 0
    while len(rows) < max_rows:
        page = min(limit, max_rows - len(rows))
        chunk = as_rows(remote.call_tool(tool, {"limit": page, "offset": offset}, timeout=timeout))
        if not chunk:
            break
        rows.extend(chunk)
        if len(chunk) < page:
            break
        offset += page
    return rows


def _rest_pages(
    url: str,
    headers: dict[str, str],
    *,
    limit: int,
    max_rows: int,
    extra: dict | None = None,
) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while len(rows) < max_rows:
        page = min(limit, max_rows - len(rows))
        params = {"limit": page, "offset": offset, **(extra or {})}
        payload = _get(url, headers, params)
        chunk = (payload or {}).get("rows", []) or []
        if not chunk:
            break
        rows.extend(chunk)
        if len(chunk) < page:
            break
        offset += page
    return rows


def _get(url: str, headers: dict[str, str], params: dict | None) -> dict:
    with httpx.Client(timeout=8.0) as client:
        response = client.get(url, headers=headers, params=params)
        if response.status_code == 429:
            response = client.get(url, headers=headers, params=params)
        response.raise_for_status()
        return response.json() if response.content else {}


def _pick(query: str, rows: list[dict], keys: tuple[str, ...]) -> list[dict]:
    tokens = re.findall(r"[а-яёa-z0-9]{4,}", (query or "").lower())
    if not tokens or not rows:
        return list(rows)
    matched = []
    for row in rows:
        hay = " ".join(str(row.get(key) or "") for key in keys).lower()
        if any(token in hay for token in tokens):
            matched.append(row)
    return matched or list(rows)


def _qty(row: dict) -> float:
    value = row.get("quantity")
    if value is None:
        value = row.get("stock")
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _open_leads(leads: list[dict]) -> list[dict]:
    open_leads = [
        lead
        for lead in leads
        if lead.get("status_id") not in {_WON, _LOST} and not lead.get("closed_at")
    ]
    open_leads.sort(key=lambda lead: str(lead.get("updated_at") or ""), reverse=True)
    return open_leads


def _chart_label(name: str, fallback: str) -> str:
    text = re.sub(r"\+?\d[\d\s\-()]{8,}", " ", name)
    text = " ".join(text.split())
    if len(text) < 3:
        return fallback[:28]
    return text[:28]


def _order_sum(order: dict) -> object:
    value = order.get("sum") or 0
    if settings.moysklad_mcp_configured:
        return value
    try:
        return float(value) / 100
    except (TypeError, ValueError):
        return 0


def _money(value: object) -> str:
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        return "сумма не указана"
    return f"{amount:,.0f} ₽".replace(",", " ")


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%d.%m.%Y %H:%M")


def _stub(source: str, title: str, path: str, excerpt: str) -> ContextHit:
    return ContextHit(source=source, title=title, excerpt=excerpt, kind="record", path=path)


def _cached(key: str):
    row = _cache.get(key)
    if row is None:
        return None
    when, value = row
    if time.monotonic() - when > _CACHE_TTL:
        return None
    return value


def _store(key: str, value: object) -> None:
    _cache[key] = (time.monotonic(), value)


def _chart_sources(*, wait: bool = True) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    if not wait:
        return _chart_sources_cached()
    leads: list[dict] = []
    stock: list[dict] = []
    orders: list[dict] = []
    tasks: list[dict] = []
    if _amocrm_configured():
        try:
            leads, _statuses = _amocrm_snapshot()
            leads = _open_leads(leads)
        except Exception as error:
            log.warning("график amoCRM: %s", error)
    if _moysklad_configured():
        try:
            stock, orders = _moysklad_snapshot()
        except Exception as error:
            log.warning("график МойСклада: %s", error)
        try:
            tasks = _production_tasks()
        except Exception as error:
            log.warning("график заданий: %s", error)
    return leads, stock, orders, tasks


def _chart_sources_cached() -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """Return whatever is already in TTL cache; never block on remote MCP/REST."""
    need_refresh = False
    leads: list[dict] = []
    stock: list[dict] = []
    orders: list[dict] = []
    tasks: list[dict] = []
    if _amocrm_configured():
        cached = _cached("amocrm")
        if cached is None:
            need_refresh = True
        else:
            leads = _open_leads(cached[0])  # type: ignore[index]
    if _moysklad_configured():
        cached = _cached("moysklad")
        if cached is None:
            need_refresh = True
        else:
            stock, orders = cached  # type: ignore[misc]
        task_cached = _cached("moysklad_tasks")
        if task_cached is None:
            need_refresh = True
        else:
            tasks = task_cached  # type: ignore[assignment]
    if need_refresh:
        _kick_chart_refresh()
    return leads, stock, orders, tasks


def _production_tasks() -> list[dict]:
    cached = _cached("moysklad_tasks")
    if cached is not None:
        return cached  # type: ignore[return-value]
    if not settings.moysklad_mcp_configured:
        return []
    rows = _mcp_pages(_moysklad_target(), "list_production_tasks", limit=50, max_rows=200, timeout=25.0)
    _store("moysklad_tasks", rows)
    return rows


def _as_float(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _parse_dt(raw: object) -> datetime | None:
    text = str(raw or "")[:19]
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _age_days(raw: object) -> int | None:
    when = _parse_dt(raw)
    if when is None:
        return None
    return max(0, (datetime.now() - when).days)


def _lead_age(lead: dict) -> int | None:
    return _age_days(lead.get("updated_at") or lead.get("created_at"))


def _chart(
    chart_id: str,
    title: str,
    unit: str,
    bars: list[dict],
    agents: list[str],
    lead: str,
    tone: str,
) -> dict:
    return {
        "id": chart_id,
        "title": title,
        "unit": unit,
        "bars": bars[:6],
        "agents": agents,
        "lead": lead,
        "tone": tone,
    }


def _coordinator_charts(leads: list[dict], stock: list[dict], orders: list[dict]) -> list[dict]:
    stuck = sum(1 for lead in leads if (age := _lead_age(lead)) is not None and age >= 30)
    unpriced = sum(1 for lead in leads if _as_float(lead.get("price")) <= 0)
    missing = sum(1 for row in stock if _qty(row) < 0)
    unpaid = sum(1 for order in orders if _as_float(order.get("sum")) - _as_float(order.get("payedSum")) > 1)
    bars = [
        {"label": "Сделки без движения >30д", "value": float(stuck)},
        {"label": "Сделки без цены", "value": float(unpriced)},
        {"label": "Позиции в минусе", "value": float(missing)},
        {"label": "Заказы без оплаты", "value": float(unpaid)},
    ]
    bars = [bar for bar in bars if bar["value"] > 0]
    if not bars:
        return []
    top = max(bars, key=lambda bar: bar["value"])
    return [
        _chart(
            "coordinator_pulse",
            "Где горит",
            "шт",
            bars,
            ["coordinator"],
            f"Главное: {top['label'].lower()} — {int(top['value'])}.",
            "warning",
        )
    ]


def _sales_charts(leads: list[dict]) -> list[dict]:
    aged = []
    for lead in leads:
        age = _lead_age(lead)
        if age is None or age < 7:
            continue
        aged.append(
            {
                "label": _chart_label(str(lead.get("name") or ""), f"сделка {lead.get('id')}"),
                "value": float(age),
            }
        )
    aged.sort(key=lambda bar: bar["value"], reverse=True)
    aged = aged[:6]
    if not aged:
        return []
    return [
        _chart(
            "sales_stuck",
            "Зависшие сделки, дни без движения",
            "дн",
            aged,
            ["sales"],
            f"Дольше всех без движения: {aged[0]['label']}, {int(aged[0]['value'])} дн.",
            "warning",
        )
    ]


def _finance_charts(leads: list[dict], orders: list[dict]) -> list[dict]:
    charts: list[dict] = []
    unpaid = 0.0
    unshipped = 0.0
    for order in orders:
        total = _as_float(_order_sum(order))
        unpaid += max(total - _as_float(order.get("payedSum")), 0)
        unshipped += max(total - _as_float(order.get("shippedSum")), 0)
    priced = sum(_as_float(lead.get("price")) for lead in leads if _as_float(lead.get("price")) > 0)
    money = [
        {"label": "Не оплачено по заказам", "value": unpaid},
        {"label": "Не отгружено", "value": unshipped},
        {"label": "Открытые сделки с суммой", "value": priced},
    ]
    money = [bar for bar in money if bar["value"] > 0]
    if money:
        top = max(money, key=lambda bar: bar["value"])
        charts.append(
            _chart(
                "finance_money",
                "Где висят деньги",
                "₽",
                money,
                ["finance"],
                f"{top['label']}: {_money(top['value'])}.",
                "timber",
            )
        )
    unpriced = sum(1 for lead in leads if _as_float(lead.get("price")) <= 0)
    priced_n = sum(1 for lead in leads if _as_float(lead.get("price")) > 0)
    if unpriced >= 5 and unpriced > priced_n:
        charts.append(
            _chart(
                "finance_unpriced",
                "Сделки без цены",
                "шт",
                [
                    {"label": "Без суммы", "value": float(unpriced)},
                    {"label": "С суммой", "value": float(priced_n)},
                ],
                ["finance"],
                f"Без цены {unpriced} открытых сделок — маржу по ним не посчитать.",
                "danger",
            )
        )
    return charts


def _warehouse_charts(stock: list[dict]) -> list[dict]:
    charts: list[dict] = []
    missing = [
        {"label": _chart_label(str(row.get("name") or row.get("code") or "позиция"), "позиция"), "value": _qty(row)}
        for row in sorted(stock, key=_qty)
        if _qty(row) < 0
    ][:6]
    if missing:
        charts.append(
            _chart(
                "warehouse_gap",
                "Минус на складе",
                "шт",
                missing,
                ["warehouse"],
                f"В минусе: {missing[0]['label']}, {missing[0]['value']:g} шт.",
                "danger",
            )
        )
    incoming = [
        {
            "label": _chart_label(str(row.get("name") or row.get("code") or "позиция"), "позиция"),
            "value": _as_float(row.get("inTransit")),
        }
        for row in stock
        if _as_float(row.get("inTransit")) > 0
    ]
    incoming.sort(key=lambda bar: bar["value"], reverse=True)
    incoming = incoming[:6]
    if incoming:
        charts.append(
            _chart(
                "warehouse_in_transit",
                "Уже едет",
                "шт",
                incoming,
                ["warehouse"],
                f"Едет: {incoming[0]['label']}, {incoming[0]['value']:g} шт.",
                "brand",
            )
        )
    return charts


def _production_charts(tasks: list[dict]) -> list[dict]:
    aged = []
    for task in tasks:
        age = _age_days(task.get("moment") or task.get("updated"))
        if age is None or age < 7:
            continue
        aged.append(
            {
                "label": _chart_label(str(task.get("name") or ""), f"ПЗ {task.get('id')}"),
                "value": float(age),
            }
        )
    aged.sort(key=lambda bar: bar["value"], reverse=True)
    aged = aged[:6]
    if not aged:
        return []
    return [
        _chart(
            "production_tasks",
            "Задания в цехе, дни",
            "дн",
            aged,
            ["production"],
            f"Дольше всех в цехе: {aged[0]['label']}, {int(aged[0]['value'])} дн.",
            "warning",
        )
    ]
