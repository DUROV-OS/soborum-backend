"""Detect a topic change so consult can drop the server transcript."""

from __future__ import annotations

import re

_ACK = re.compile(
    r"^(ок|okay|ok|да|нет|хорошо|понял|поняла|спасибо|благодар|угу|ага|ясно)[\s!.…]*$",
    re.IGNORECASE,
)
_CLUSTERS = (
    ("склад", "остат", "минус", "позиц", "материал", "саморез", "вентил", "доск"),
    ("сделк", "клиент", "амо", "воронк", "лид", "оплат"),
    ("цен", "скидк", "ипотек", "руб", "договор", "марж"),
    ("цех", "производ", "задани", "техкарт", "дом", "барн", "модул"),
    ("юрист", "закон", "конкурент", "ворован", "персональн"),
)


def topic_shifted(previous: list[str], incoming: str) -> bool:
    text = (incoming or "").strip()
    if not text or not previous:
        return False
    if _ACK.match(text) or len(text) < 8:
        return False
    prior = " ".join(previous[-6:])
    old_clusters = _clusters(prior)
    new_clusters = _clusters(text)
    if new_clusters and old_clusters:
        return new_clusters.isdisjoint(old_clusters)
    old_tokens = _tokens(prior)
    new_tokens = _tokens(text)
    if len(new_tokens) < 3 or not old_tokens:
        return False
    overlap = len(old_tokens & new_tokens) / len(new_tokens)
    return overlap < 0.18


def _clusters(text: str) -> set[int]:
    low = text.lower()
    return {index for index, words in enumerate(_CLUSTERS) if any(word in low for word in words)}


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[а-яёa-z0-9]{4,}", text.lower()))
