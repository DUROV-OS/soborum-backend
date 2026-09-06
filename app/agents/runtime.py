"""Coordinator turn without an LLM and without vault gather().

Specialists answer from the role passport. Vault and CRM wiring is a later step.
"""

from __future__ import annotations

import uuid

from app.agents.ids import DOES_NOT_OWN, RU_LABELS, AgentId
from app.agents.legal import lawyer_reply, scan
from app.agents.routing import route
from app.agents.types import LegalDecision, LegalVerdict, Opinion, RunResult


def run_task(text: str) -> RunResult:
    if not text or not text.strip():
        raise ValueError("Пустой запрос")

    legal = scan(text)
    planned = route(text, legal)
    opinions: list[Opinion] = []

    if legal.verdict == LegalVerdict.BLOCK:
        opinions.append(_speak(AgentId.LAWYER, text, legal))
        return RunResult(
            reply=_blocked_reply(opinions[0], legal),
            route=planned,
            legal=legal,
            opinions=opinions,
            released=False,
            trace_id=_trace_id(),
        )

    for agent_id in planned.specialists:
        if agent_id == AgentId.LAWYER:
            continue
        opinions.append(_speak(agent_id, text, legal))

    if legal.needs_lawyer or AgentId.LAWYER in planned.specialists:
        opinions.append(_speak(AgentId.LAWYER, text, legal))

    released = legal.verdict == LegalVerdict.ALLOW
    return RunResult(
        reply=_synthesize(text, opinions, legal, released),
        route=planned,
        legal=legal,
        opinions=opinions,
        released=released,
        trace_id=_trace_id(),
    )


def _speak(agent_id: AgentId, text: str, legal: LegalDecision) -> Opinion:
    if agent_id == AgentId.LAWYER:
        return Opinion(agent=agent_id, stance=lawyer_reply(legal))
    return Opinion(
        agent=agent_id,
        stance=(
            f"{RU_LABELS[agent_id].capitalize()}. По запросу «{_one_line(text)}». "
            f"Не моё: {DOES_NOT_OWN[agent_id]}. "
            "Живой vault ещё не подключён — опираюсь на паспорт роли."
        ),
    )


def _blocked_reply(lawyer: Opinion, legal: LegalDecision) -> str:
    return (
        f"{lawyer.stance}\n\n"
        f"Координатор не передаёт задачу специалистам: legal gate = {legal.verdict}. "
        f"Правила: {', '.join(f.rule_id for f in legal.findings)}."
    )


def _synthesize(text: str, opinions: list[Opinion], legal: LegalDecision, released: bool) -> str:
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
    return "\n".join(lines)


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _trace_id() -> str:
    return uuid.uuid4().hex
