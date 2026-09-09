"""Read-only live tools for Marina consult. Same source as the agent shift:
the DurovOS database itself (clients, cycle, production, warehouse, tasks,
marketing) — no CRM, no МойСклад, no external sync."""

from app.agents import connectors
from app.agents.vault import LocalVaultAdapter
from app.ai.models import ChatDomain
from app.ai.tools import register
from app.common.module_access import Module
from app.core.config import settings


@register(
    "live_company_pulse",
    "Живой срез базы DurovOS: клиенты по стадиям, цикл, производство, склад, задачи, "
    "маркетинг, финансы — тот же источник, что у смены агентов. Вызывай, чтобы освежить числа.",
    {},
    required_module=Module.AI,
    read_only=True,
    domains=[ChatDomain.GENERAL],
)
def _live_company_pulse(db, user) -> dict:
    return connectors.live_briefing(db)


@register(
    "live_clients_and_money",
    "Клиенты и деньги из базы DurovOS: сколько клиентов по стадиям, кто ждёт "
    "подтверждения оплаты и остатка, портфель и скидки сверх 5%.",
    {},
    required_module=Module.AI,
    read_only=True,
    domains=[ChatDomain.GENERAL],
)
def _live_clients_and_money(db, user) -> dict:
    data = connectors.live_briefing(db)
    return {
        "source": "durovos_database",
        "clients": data.get("clients", {}),
        "finance": data.get("finance", {}),
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
