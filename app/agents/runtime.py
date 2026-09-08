"""Coordinator turn: legal scan → route → vault gather → specialist stance."""

from __future__ import annotations

import uuid

from app.agents.context import gather
from app.agents.ids import DOES_NOT_OWN, RU_LABELS, VAULT_PATHS, AgentId
from app.agents.legal import lawyer_reply, scan
from app.agents.routing import route
from app.agents.types import ContextHit, LegalDecision, LegalVerdict, Opinion, RunResult, SharedContext
from app.agents.vault import ALWAYS_PATHS


def run_task(text: str, vault_root: str | None = None) -> RunResult:
    if not text or not text.strip():
        raise ValueError("Пустой запрос")

    legal = scan(text)
    planned = route(text, legal)
    context = gather(text, planned.specialists, vault_root)
    opinions: list[Opinion] = []

    if legal.verdict == LegalVerdict.BLOCK:
        opinions.append(_speak(AgentId.LAWYER, text, legal, context))
        return RunResult(
            reply=_blocked_reply(opinions[0], legal, context),
            route=planned,
            legal=legal,
            opinions=opinions,
            context=context,
            released=False,
            trace_id=_trace_id(),
        )

    for agent_id in planned.specialists:
        if agent_id == AgentId.LAWYER:
            continue
        opinions.append(_speak(agent_id, text, legal, context))

    if legal.needs_lawyer or AgentId.LAWYER in planned.specialists:
        opinions.append(_speak(AgentId.LAWYER, text, legal, context))

    released = legal.verdict == LegalVerdict.ALLOW
    return RunResult(
        reply=_synthesize(text, opinions, legal, released, context),
        route=planned,
        legal=legal,
        opinions=opinions,
        context=context,
        released=released,
        trace_id=_trace_id(),
    )


def _speak(agent_id: AgentId, text: str, legal: LegalDecision, context: SharedContext) -> Opinion:
    if agent_id == AgentId.LAWYER:
        return Opinion(agent=agent_id, stance=lawyer_reply(legal), citations=["legal_gate"])

    relevant = _rank_hits(agent_id, context.hits)
    citations = [f"{hit.title} ({hit.path})" if hit.path else hit.title for hit in relevant[:3]]
    if not citations:
        citations = ["общий контекст компании не дал профильного факта"]
    excerpt = next((hit.excerpt for hit in relevant if hit.excerpt), "")
    stance = (
        f"{RU_LABELS[agent_id].capitalize()}. По запросу «{_one_line(text)}». "
        f"Опираюсь на: {'; '.join(citations)}. "
        + (f"{excerpt} " if excerpt else "")
        + f"Не моё: {DOES_NOT_OWN[agent_id]}."
    )
    return Opinion(agent=agent_id, stance=stance, citations=citations)


def _rank_hits(agent_id: AgentId, hits: list[ContextHit]) -> list[ContextHit]:
    live = [hit for hit in hits if _live_hit(agent_id, hit)]
    profile = [hit for hit in hits if _profile_hit(agent_id, hit) and hit not in live]
    always = [hit for hit in hits if hit.path in ALWAYS_PATHS and hit not in profile and hit not in live]
    other = [
        hit
        for hit in hits
        if _useful(agent_id, hit) and hit not in live and hit not in profile and hit not in always
    ]
    return live + profile + other + always


def _live_hit(agent_id: AgentId, hit: ContextHit) -> bool:
    path = hit.path or ""
    if hit.source == "crm" and path.startswith("amocrm/") and agent_id in {
        AgentId.COORDINATOR,
        AgentId.SALES,
        AgentId.MARKETER,
        AgentId.FINANCE,
    }:
        return True
    if hit.source == "warehouse" and path.startswith("moysklad/") and agent_id in {
        AgentId.COORDINATOR,
        AgentId.WAREHOUSE,
        AgentId.PRODUCTION,
        AgentId.FINANCE,
    }:
        return True
    return False


def _profile_hit(agent_id: AgentId, hit: ContextHit) -> bool:
    path = hit.path
    return bool(path) and any(path.startswith(prefix) for prefix in VAULT_PATHS[agent_id]) and path not in ALWAYS_PATHS


def _useful(agent_id: AgentId, hit: ContextHit) -> bool:
    if _live_hit(agent_id, hit):
        return True
    path = hit.path
    if not path:
        return False
    if path in ALWAYS_PATHS or path.startswith("02_Business/00_Decision_Log/"):
        return True
    return any(path.startswith(prefix) for prefix in VAULT_PATHS[agent_id])


def _blocked_reply(lawyer: Opinion, legal: LegalDecision, context: SharedContext) -> str:
    lines = [
        lawyer.stance,
        "",
        f"Координатор не передаёт задачу специалистам: legal gate = {legal.verdict}. "
        f"Правила: {', '.join(f.rule_id for f in legal.findings)}.",
    ]
    lines.extend(_sources(context))
    return "\n".join(lines)


def _synthesize(
    text: str,
    opinions: list[Opinion],
    legal: LegalDecision,
    released: bool,
    context: SharedContext,
) -> str:
    lines = [
        "Координатор.",
        f"Запрос: {_one_line(text)}",
        f"Legal gate: {legal.verdict}.",
    ]
    if opinions:
        lines.append("Специалисты:")
        for opinion in opinions:
            lines.append(f"- {RU_LABELS[opinion.agent]}: {opinion.stance}")
    if not released:
        lines.append(
            "Ответ не выпущен как действие. Нужен человек."
            if legal.verdict == LegalVerdict.ESCALATE_HUMAN
            else "Ответ не выпущен."
        )
    else:
        lines.append("Выпуск: информационный. Любое изменение данных — подтверждение человека.")
    lines.extend(_sources(context))
    return "\n".join(lines)


def _sources(context: SharedContext) -> list[str]:
    vault_hits = [hit for hit in context.hits if hit.source == "vault" and hit.path]
    if not vault_hits:
        return ["Источники: vault не подключён (задайте VAULT_ROOT на checkout vault_backups)."]
    profile = [hit for hit in vault_hits if hit.path not in ALWAYS_PATHS]
    always = [hit for hit in vault_hits if hit.path in ALWAYS_PATHS]
    ordered = profile + always
    lines = ["Источники:"]
    for hit in ordered[:10]:
        lines.append(f"- {hit.title} · {hit.path}")
    return lines


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _trace_id() -> str:
    return uuid.uuid4().hex
