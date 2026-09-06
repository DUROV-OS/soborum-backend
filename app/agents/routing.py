"""Deterministic router. Coordinator always owns the turn.

A later LLM classifier must beat this keyword baseline on gold before replacing it.
"""

from __future__ import annotations

import re

from app.agents.ids import AgentId
from app.agents.types import LegalDecision, LegalVerdict, Route

KEYWORDS: dict[AgentId, tuple[str, ...]] = {
    AgentId.SALES: (
        "сделк",
        "лид",
        "клиент",
        "воронк",
        "кп ",
        "коммерческ",
        "звонок",
        "встреч",
        "оплат",
        "ипотек",
        "продаж",
    ),
    AgentId.MARKETER: (
        "пост",
        "контент",
        "реклам",
        " instagram",
        "телеграм",
        "вконтакте",
        "лидген",
        "оффер",
        "позиционир",
        "таргет",
        "директ",
    ),
    AgentId.PRODUCTION: (
        "цех",
        "производ",
        "модул",
        "сборк",
        "загрузк",
        "срок изготов",
        "смен",
        "бригад",
    ),
    AgentId.WAREHOUSE: (
        "склад",
        "остат",
        "поставк",
        "номенклатур",
        "материал",
        "заявк",
        "приход",
        "комплектующ",
    ),
    AgentId.FINANCE: (
        "марж",
        "скидк",
        "себестоим",
        "юнит",
        "экономик",
        "прибыл",
        "оплат",
        "счет",
        "счёт",
        "касс",
    ),
    AgentId.LAWYER: (
        "договор",
        "претензи",
        "юрист",
        "закон",
        "пдн",
        "иск",
        "конкурент",
        "ворован",
        "слит",
    ),
    AgentId.ENGINEER: (
        "чертёж",
        "чертеж",
        "техкарт",
        "гост",
        "снип",
        "нагрузк",
        "узел",
        "несущ",
        "конструктив",
        "спецификац",
    ),
}

COMPETITOR_OPEN = re.compile(r"конкурент|рынок|сравн", re.IGNORECASE)


def _score(text: str) -> dict[AgentId, int]:
    lowered = f" {text.lower()} "
    return {agent_id: sum(1 for word in words if word in lowered) for agent_id, words in KEYWORDS.items()}


def route(text: str, legal: LegalDecision) -> Route:
    scores = _score(text)
    ranked = sorted(
        ((agent_id, score) for agent_id, score in scores.items() if score > 0),
        key=lambda item: (-item[1], item[0].value),
    )
    specialists = [agent_id for agent_id, _ in ranked[:3]]

    if legal.verdict != LegalVerdict.ALLOW or legal.needs_lawyer:
        if AgentId.LAWYER not in specialists:
            specialists.insert(0, AgentId.LAWYER)

    if COMPETITOR_OPEN.search(text) and AgentId.MARKETER not in specialists:
        if AgentId.SALES in specialists or "конкурент" in text.lower():
            specialists.append(AgentId.MARKETER)

    if not specialists:
        specialists = [AgentId.SALES]

    specialists = [agent_id for agent_id in specialists if agent_id != AgentId.COORDINATOR]

    reason = "маршрут по ключевым словам"
    if legal.verdict != LegalVerdict.ALLOW:
        reason += f"; legal={legal.verdict}"
    return Route(specialists=specialists, scores=scores, reason=reason)
