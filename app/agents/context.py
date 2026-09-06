"""Shared company context from a GitHub checkout of vault_backups."""

from __future__ import annotations

from app.agents.ids import VAULT_PATHS, AgentId
from app.agents.types import ContextHit, SharedContext
from app.agents.vault import LocalVaultAdapter, vault_root_from_env
from app.core.config import settings

POLICY = {
    "discount_autonomy_pct": "5",
    "final_price": "human_approval",
    "competitor_intel": "open_sources_only",
    "crm_role": "primary_collection_then_vault",
    "source_of_truth": "vault_backups",
}


def gather(text: str, agents: list[AgentId], vault_root: str | None = None) -> SharedContext:
    root = vault_root or settings.vault_root or (str(path) if (path := vault_root_from_env()) else "")
    hits: list[ContextHit] = []
    if root:
        hits.extend(LocalVaultAdapter(root).gather(text, _prefixes(agents)))
    hits.extend(_pending_connectors(agents))
    return SharedContext(hits=_dedup(hits), policy=dict(POLICY))


def _prefixes(agents: list[AgentId]) -> list[str]:
    prefixes = {"02_Business/00_Decision_Log"}
    for agent_id in agents:
        prefixes.update(VAULT_PATHS[agent_id])
    return sorted(prefixes)


def _pending_connectors(agents: list[AgentId]) -> list[ContextHit]:
    hits: list[ContextHit] = []
    if any(agent_id in {AgentId.SALES, AgentId.MARKETER, AgentId.FINANCE} for agent_id in agents):
        hits.append(
            ContextHit(
                source="crm",
                title="amoCRM",
                excerpt="Коннектор ещё не вшит в gather(). Живые сделки появятся после чтения MCP; истина — 03_Clients.",
                kind="record",
                path="amocrm",
            )
        )
    if any(agent_id in {AgentId.WAREHOUSE, AgentId.PRODUCTION, AgentId.FINANCE} for agent_id in agents):
        hits.append(
            ContextHit(
                source="warehouse",
                title="МойСклад",
                excerpt="Коннектор ещё не вшит в gather(). Остатки читаются после проверки; факт уходит в базу.",
                kind="record",
                path="moysklad",
            )
        )
    return hits


def _dedup(hits: list[ContextHit]) -> list[ContextHit]:
    seen: set[tuple[str, str]] = set()
    unique: list[ContextHit] = []
    for hit in hits:
        key = (hit.source, hit.path or hit.title)
        if key in seen:
            continue
        seen.add(key)
        unique.append(hit)
    return unique
