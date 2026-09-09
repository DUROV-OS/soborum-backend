"""Shared company context: the DurovOS database plus a GitHub checkout of vault_backups."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.agents.connectors import live_hits
from app.agents.ids import VAULT_PATHS, AgentId
from app.agents.types import ContextHit, SharedContext
from app.agents.vault import LocalVaultAdapter, vault_root_from_env
from app.core.config import settings

POLICY = {
    "discount_autonomy_pct": "5",
    "final_price": "human_approval",
    "competitor_intel": "open_sources_only",
    "live_source": "durovos_database",
    "source_of_truth": "durovos_database_and_vault_backups",
}


def gather(
    text: str,
    agents: list[AgentId],
    vault_root: str | None = None,
    db: Session | None = None,
) -> SharedContext:
    root = vault_root or settings.vault_root or (str(path) if (path := vault_root_from_env()) else "")
    hits: list[ContextHit] = []
    hits.extend(live_hits(text, agents, db))
    if root:
        hits.extend(LocalVaultAdapter(root).gather(text, _prefixes(agents)))
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
