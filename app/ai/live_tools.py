"""Read-only live CRM/stock/vault tools for Marina consult. Same sources as the shift."""

from app.agents import connectors
from app.agents.vault import LocalVaultAdapter
from app.ai.models import ChatDomain
from app.ai.tools import register
from app.common.module_access import Module
from app.core.config import settings


@register(
    "live_company_pulse",
    "Сводка живого среза amoCRM + МойСклад, тот же что у смены агентов. "
    "Вызывай, если нужно освежить числа по сделкам, складу, заказам и цеху.",
    {},
    required_module=Module.AI,
    read_only=True,
    domains=[ChatDomain.GENERAL],
)
def _live_company_pulse(db, user) -> dict:
    return connectors.live_briefing()


@register(
    "live_stock_gaps",
    "Позиции МойСклад с минусом или нулём. Это операционный склад, не модуль Soborbum.",
    {"limit": {"type": "integer"}},
    required_module=Module.AI,
    read_only=True,
    domains=[ChatDomain.GENERAL],
)
def _live_stock_gaps(db, user, limit: int = 20) -> dict:
    data = connectors.live_briefing()
    return {
        "source": "moysklad",
        "stock_rows": data["stock_rows"],
        "negatives": data["negatives"],
        "items": data["negative_items"][: max(1, min(limit, 40))],
    }


@register(
    "live_stuck_deals",
    "Открытые сделки amoCRM без движения. Не выдумывай имена — только этот список.",
    {"limit": {"type": "integer"}},
    required_module=Module.AI,
    read_only=True,
    domains=[ChatDomain.GENERAL],
)
def _live_stuck_deals(db, user, limit: int = 10) -> dict:
    data = connectors.live_briefing()
    return {
        "source": "amocrm",
        "open_deals": data["open_deals"],
        "unpriced": data["unpriced"],
        "stuck_30d": data["stuck_30d"],
        "deals": data["stuck_deals"][: max(1, min(limit, 20))],
    }


@register(
    "live_shop_and_orders",
    "Заказы покупателей и производственные задания из МойСклад.",
    {},
    required_module=Module.AI,
    read_only=True,
    domains=[ChatDomain.GENERAL],
)
def _live_shop_and_orders(db, user) -> dict:
    data = connectors.live_briefing()
    return {
        "source": "moysklad",
        "orders": data["orders"],
        "unpaid_orders": data["unpaid_orders"],
        "tasks": data["tasks"],
        "task_rows": data["task_rows"],
    }


@register(
    "search_company_vault",
    "Поиск в vault компании (техкарты, правила, факты). Не путать с живым МойСклад.",
    {"query": {"type": "string"}},
    ["query"],
    required_module=Module.AI,
    read_only=True,
    domains=[ChatDomain.GENERAL],
)
def _search_company_vault(db, user, query: str) -> dict:
    root = settings.vault_root
    if not root:
        return {"hits": [], "detail": "VAULT_ROOT не задан."}
    hits = LocalVaultAdapter(root).search(query, limit=6)
    return {
        "hits": [
            {"title": hit.title, "path": hit.path, "excerpt": hit.excerpt[:400], "kind": hit.kind}
            for hit in hits
        ]
    }