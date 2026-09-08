"""Read-only live МойСклад/vault tools for Marina consult. Same source as the shift:
customer orders (bookkeeping) only — no CRM, no stock levels, no production tasks."""

from app.agents import connectors
from app.agents.vault import LocalVaultAdapter
from app.ai.models import ChatDomain
from app.ai.tools import register
from app.common.module_access import Module
from app.core.config import settings


@register(
    "live_company_pulse",
    "Сводка живого среза: заказы покупателей МойСклад (суммы, оплаты, отгрузки), "
    "тот же источник что у смены агентов. Вызывай, если нужно освежить числа по заказам и оплатам.",
    {},
    required_module=Module.AI,
    read_only=True,
    domains=[ChatDomain.GENERAL],
)
def _live_company_pulse(db, user) -> dict:
    return connectors.live_briefing()


@register(
    "live_shop_and_orders",
    "Заказы покупателей из МойСклад: суммы, оплачено, отгружено, сколько без оплаты.",
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
    }


@register(
    "search_company_vault",
    "Поиск в vault компании (техкарты, правила, факты).",
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
