"""Read-only МойСклад snapshot for gather().

Only customer orders (bookkeeping: sums, paid, shipped). No CRM, no stock
levels, no production tasks. Live numbers are freshness, weaker than a later
vault `kind: fact`. This client only GETs. A missing token stays an honest stub.
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

# Roles that see money/orders. No stock or CRM roles anymore.
_ORDER_ROLES = {AgentId.COORDINATOR, AgentId.FINANCE}


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
    """Compact live snapshot for Marina consult. Same source as the shift:
    МойСклад customer orders only.

    wait=False: only cached orders (kick background refresh). Use this in the
    consult system prompt so a cold МойСклад dump cannot block Claude for ~30s.
    """
    orders = _chart_sources(wait=wait)
    unpaid = [
        order
        for order in orders
        if _as_float(_order_sum(order)) - _as_float(order.get("payedSum")) > 1
    ]
    return {
        "orders_connected": _moysklad_configured(),
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
    }


def live_briefing_text(*, wait: bool = False) -> str:
    data = live_briefing(wait=wait)
    if not data["orders_connected"]:
        return (
            "Живой срез: МойСклад не подключён. "
            "Не выдумывай заказы, суммы и оплаты."
        )
    if not data["orders"]:
        return (
            "Живой срез МойСклад сейчас пуст или ещё собирается. "
            "Не утверждай про оплаты и отгрузки — данных нет."
        )
    unpaid_money = sum(max(order["sum"] - order["payed"], 0) for order in data["orders"])
    return (
        "Живой срез компании — заказы покупателей МойСклад (бухгалтерия). "
        "Слабее позднего факта в vault.\n"
        f"Заказы: {len(data['orders'])}, без оплаты {data['unpaid_orders']}, "
        f"не оплачено {_money(unpaid_money)}.\n"
        "Пиши по этим числам. Складских остатков и сделок CRM здесь нет — не выдумывай их."
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
    orders = _chart_sources()
    charts = [
        *_coordinator_charts(orders),
        *_finance_charts(orders),
    ]
    charts = [chart for chart in charts if chart.get("bars")]
    _charts_ready = charts
    _store("charts_ready", charts)
    return charts


def live_hits(query: str, agents: list[AgentId]) -> list[ContextHit]:
    if not any(agent in _ORDER_ROLES for agent in agents):
        return []
    return _moysklad_hits(query)


def _moysklad_configured() -> bool:
    return settings.moysklad_mcp_configured or bool(settings.moysklad_token)


def _moysklad_target() -> McpTarget:
    return McpTarget(
        name="moysklad",
        url=settings.moysklad_mcp_url,
        client_id=settings.moysklad_mcp_client_id,
        client_secret=settings.moysklad_mcp_client_secret,
        scope=settings.moysklad_mcp_scope,
        redirect_uri=settings.mcp_redirect_uri,
    )


def _moysklad_hits(query: str) -> list[ContextHit]:
    if not _moysklad_configured():
        return [_stub("warehouse", "МойСклад", "moysklad", "МойСклад не задан. Заказы и оплаты не выдумываем.")]
    try:
        orders = _moysklad_snapshot()
    except Exception as error:
        log.warning("МойСклад не прочитался: %s", error)
        return [_stub("warehouse", "МойСклад", "moysklad", f"МойСклад не ответил: {error}. Заказы не выдумываем.")]

    now = _now()
    hits: list[ContextHit] = []
    picked_orders = _pick(query, orders, keys=("name",)) or orders[:5]
    for order in picked_orders[:5]:
        name = str(order.get("name") or "заказ")
        total = _as_float(_order_sum(order))
        payed = _as_float(order.get("payedSum"))
        hits.append(
            ContextHit(
                source="warehouse",
                title=f"МойСклад заказ: {name}",
                excerpt=(
                    f"{name}, сумма {_money(total)}, оплачено {_money(payed)}, "
                    f"дата {order.get('moment') or '—'}. Снимок {now}."
                ),
                kind="record",
                path=f"moysklad/customerorder/{order.get('id')}",
            )
        )
    if not hits:
        hits.append(_stub("warehouse", "МойСклад", "moysklad/empty", f"Заказов в отчёте нет. Снимок {now}."))
    return hits


def _moysklad_snapshot() -> list[dict]:
    """Customer orders only. Returns a list of order dicts."""
    cached = _cached("moysklad")
    if cached is not None:
        return cached  # type: ignore[return-value]
    if settings.moysklad_mcp_configured:
        orders = _mcp_pages(_moysklad_target(), "list_customer_orders", limit=50, max_rows=200, timeout=30.0)
        _store("moysklad", orders)
        return orders
    headers = {"Authorization": f"Bearer {settings.moysklad_token}", "Accept-Encoding": "gzip"}
    base = "https://api.moysklad.ru/api/remap/1.2"
    orders = _rest_pages(
        f"{base}/entity/customerorder", headers, limit=50, max_rows=200, extra={"order": "moment,desc"}
    )
    _store("moysklad", orders)
    return orders


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


def _chart_sources(*, wait: bool = True) -> list[dict]:
    if not wait:
        return _chart_sources_cached()
    orders: list[dict] = []
    if _moysklad_configured():
        try:
            orders = _moysklad_snapshot()
        except Exception as error:
            log.warning("график МойСклада: %s", error)
    return orders


def _chart_sources_cached() -> list[dict]:
    """Return whatever is already in TTL cache; never block on remote MCP/REST."""
    if not _moysklad_configured():
        return []
    cached = _cached("moysklad")
    if cached is None:
        _kick_chart_refresh()
        return []
    return cached  # type: ignore[return-value]


def _as_float(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


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


def _coordinator_charts(orders: list[dict]) -> list[dict]:
    unpaid = sum(
        1
        for order in orders
        if _as_float(_order_sum(order)) - _as_float(order.get("payedSum")) > 1
    )
    bars = [{"label": "Заказы без оплаты", "value": float(unpaid)}]
    bars = [bar for bar in bars if bar["value"] > 0]
    if not bars:
        return []
    return [
        _chart(
            "coordinator_pulse",
            "Где горит",
            "шт",
            bars,
            ["coordinator"],
            f"Заказов без оплаты — {unpaid}.",
            "warning",
        )
    ]


def _finance_charts(orders: list[dict]) -> list[dict]:
    unpaid = 0.0
    unshipped = 0.0
    for order in orders:
        total = _as_float(_order_sum(order))
        unpaid += max(total - _as_float(order.get("payedSum")), 0)
        unshipped += max(total - _as_float(order.get("shippedSum")), 0)
    money = [
        {"label": "Не оплачено по заказам", "value": unpaid},
        {"label": "Не отгружено", "value": unshipped},
    ]
    money = [bar for bar in money if bar["value"] > 0]
    if not money:
        return []
    top = max(money, key=lambda bar: bar["value"])
    return [
        _chart(
            "finance_money",
            "Где висят деньги",
            "₽",
            money,
            ["finance"],
            f"{top['label']}: {_money(top['value'])}.",
            "timber",
        )
    ]
