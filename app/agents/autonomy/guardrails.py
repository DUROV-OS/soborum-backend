"""Ограничители автономного агента (0112) — в коде, а не только в промпте.

Решают, на каком уровне действие реально выполняется, и запрещают то, что агенту
нельзя вообще. Неуверенность всегда ведёт к уровню Б.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.agents.autonomy.models import LEVEL_A, LEVEL_B
from app.agents.autonomy.playbook import Finding
from app.agents.autonomy.workspace import must_ask_human
from app.common.module_access import Module

# Что А-действию трогать нельзя: деньги и права. Попытка — ошибка разработчика, а не
# повод «разрешить исключение», поэтому бросаем исключение.
FORBIDDEN_MODULES = {Module.ACCOUNTING}
FORBIDDEN_ACTION_KINDS = {"money", "access_change", "delete", "external_send"}


class GuardrailViolation(RuntimeError):
    pass


def assert_allowed(kind: str, module: Module | None = None) -> None:
    if kind in FORBIDDEN_ACTION_KINDS:
        raise GuardrailViolation(f"Агенту запрещено действие «{kind}»")
    if module in FORBIDDEN_MODULES:
        raise GuardrailViolation(f"Агенту запрещено писать в раздел «{module.value}»")


def resolve_level(db: Session, finding: Finding, ask_human: bool | None = None) -> tuple[str, str]:
    """Итоговый уровень и причина понижения (пустая строка, если уровень не менялся)."""
    if finding.uncertain:
        return LEVEL_B, "данные противоречивы — карточка с вопросом"
    if finding.external:
        return LEVEL_B, "требует внешней отправки — только после подтверждения"
    if finding.level != LEVEL_A:
        return LEVEL_B, ""
    if must_ask_human(db, finding.scenario) if ask_human is None else ask_human:
        return LEVEL_B, "повторялось без реакции человека — перевожу на подтверждение"
    return LEVEL_A, ""
