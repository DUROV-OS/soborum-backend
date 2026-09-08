"""Shared company context from a GitHub checkout of vault_backups."""

from __future__ import annotations

from app.agents.connectors import live_hits
from app.agents.ids import VAULT_PATHS, AgentId
from app.agents.types import ContextHit, SharedContext
from app.agents.vault import LocalVaultAdapter, vault_root_from_env
from app.core.config import settings

POLICY = {
    "discount_autonomy_pct": "5",
    "final_price": "human_approval",
    "competitor_intel": "open_sources_only",
    "live_source": "moysklad_customer_orders_only",
    "source_of_truth": "vault_backups",
}


def gather(text: str, agents: list[AgentId], vault_root: str | None = None) -> SharedContext:
    root = vault_root or settings.vault_root or (str(path) if (path := vault_root_from_env()) else "")
    hits: list[ContextHit] = []
    if root:
        hits.extend(LocalVaultAdapter(root).gather(text, _prefixes(agents)))
    hits.extend(live_hits(text, agents))
    return SharedContext(hits=_dedup(hits), policy=dict(POLICY))


def _prefixes(agents: list[AgentId]) -> list[str]:
    prefixes = {"02_Business/00_Decision_Log"}
    for agent_id in agents:
        prefixes.update(VAULT_PATHS[agent_id])
    return sorted(prefixes)


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
