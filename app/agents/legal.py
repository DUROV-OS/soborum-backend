"""Deterministic legal filter. Copied from the public agents runtime.

An LLM, if added later, may only tighten a verdict. It cannot loosen a block.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.agents.types import LegalCategory, LegalDecision, LegalFinding, LegalVerdict


@dataclass(frozen=True)
class Rule:
    id: str
    category: LegalCategory
    verdict: LegalVerdict
    patterns: tuple[str, ...]
    reason: str
    human_line: str


RULES: tuple[Rule, ...] = (
    Rule(
        id="stolen-competitor-intel",
        category=LegalCategory.COMPETITOR_INTEL_ILLEGAL,
        verdict=LegalVerdict.BLOCK,
        patterns=(
            r"ворованн",
            r"украденн",
            r"слит\w*\s+(баз|клиент|прайс|воронк)",
            r"инсайд(ерск|ер)",
            r"внутрення(я|юю)\s+(баз|crm|воронк|переписк).{0,40}конкурент",
            r"баз[ауие]\s+клиентов\s+конкурент",
            r"украсть\s+.{0,40}конкурент",
            r"парс(ить|инг).{0,40}(закрыт|личн|кабинет|crm)",
            r"сотрудник\s+конкурент.{0,40}(слил|принёс|передал)",
        ),
        reason=(
            "Конкурентная разведка допустима только по открытым источникам. "
            "Ворованные, слитые и инсайдерские данные конкурентов использовать нельзя."
        ),
        human_line=(
            "Стоп. Ворованную или слитую информацию конкурентов использовать нельзя. "
            "Работаем только с открытыми источниками и своими данными."
        ),
    ),
    Rule(
        id="contract-obligation",
        category=LegalCategory.CONTRACT,
        verdict=LegalVerdict.ESCALATE_HUMAN,
        patterns=(
            r"договор",
            r"оферт",
            r"подпис(ать|ь|ание)",
            r"претензи",
            r"\bиск\b",
            r"расторг",
            r"эскроу",
        ),
        reason="Юридические обязательства требует Human Approval.",
        human_line="Договор, претензия или подпись — только после человека. Юрист готовит паспорт, не подписывает.",
    ),
    Rule(
        id="personal-data",
        category=LegalCategory.PERSONAL_DATA,
        verdict=LegalVerdict.ESCALATE_HUMAN,
        patterns=(
            r"\bпдн\b",
            r"персональн(ые|ых)\s+данн",
            r"паспортн",
            r"снилс",
            r"запись\s+звонк",
        ),
        reason="Персональные данные третьих лиц не кладутся в базу и не обрабатываются без правового основания.",
        human_line="ПДн: не складывать в чат и базу. Нужна правовая рамка и человек.",
    ),
    Rule(
        id="discount-over-limit",
        category=LegalCategory.PRICING_AUTHORITY,
        verdict=LegalVerdict.ESCALATE_HUMAN,
        patterns=(
            r"скидк\w*\s+(сверх|больше|выше)?\s*\d{2,}",
            r"скидк\w*\s+\d{2,}\s*%",
            r"скидк\w*.{0,20}(10|15|20|25|30)\s*%",
            r"окончательн(ая|ую)\s+цен",
            r"отдать\s+по\s+себестоимост",
        ),
        reason="Окончательная цена и скидка сверх 5% — только с подтверждением человека.",
        human_line="Цена или скидка сверх лимита 5% — эскалация владельцу, не автономное решение агента.",
    ),
    Rule(
        id="owner-promise",
        category=LegalCategory.OWNER_PROMISE,
        verdict=LegalVerdict.ESCALATE_HUMAN,
        patterns=(
            r"от\s+имени\s+(владельц|игор|директор|дуров)",
            r"пообеща(й|ть|ем)\s+от\s+имени",
            r"гарантируем\s+от\s+компании",
        ),
        reason="Обещания от имени владельца требуют его подтверждения.",
        human_line="От имени владельца агент не обещает.",
    ),
    Rule(
        id="labor",
        category=LegalCategory.LABOR,
        verdict=LegalVerdict.ESCALATE_HUMAN,
        patterns=(
            r"уволи",
            r"найм",
            r"оклад",
            r"зарплат",
            r"трудов(ой|ого)\s+договор",
        ),
        reason="Кадры и оплата труда — всегда человек.",
        human_line="Найм, увольнение, оплата труда — не зона агентов.",
    ),
    Rule(
        id="safety-code",
        category=LegalCategory.SAFETY_CODE,
        verdict=LegalVerdict.ESCALATE_HUMAN,
        patterns=(
            r"несущ(ая|ую|ей)\s+стен",
            r"нагрузк",
            r"эвакуац",
            r"пожарн",
            r"снип",
            r"\bгост\b",
            r"\bсп\s?\d",
        ),
        reason="Нормы и безопасность требуют инженерного паспорта и, при риске, человека.",
        human_line="Нормы и несущие решения — инженер + человек, не «давайте так поставим».",
    ),
    Rule(
        id="ip-unauthorized",
        category=LegalCategory.INTELLECTUAL_PROPERTY,
        verdict=LegalVerdict.BLOCK,
        patterns=(
            r"скопир(уем|овать)\s+(чертёж|чертеж|проект)\s+конкурент",
            r"укра(сть|дём)\s+(проект|чертёж|чертеж)",
            r"без\s+лицензи.{0,20}(чуж|конкурент)",
        ),
        reason="Чужой проект и чертёж без права использования — запрещены.",
        human_line="Чужой проект или чертёж без права — блок.",
    ),
)

_COMPILED = tuple((rule, tuple(re.compile(p, re.IGNORECASE) for p in rule.patterns)) for rule in RULES)

_VERDICT_RANK = {
    LegalVerdict.ALLOW: 0,
    LegalVerdict.ALLOW_WITH_CONDITIONS: 1,
    LegalVerdict.ESCALATE_HUMAN: 2,
    LegalVerdict.BLOCK: 3,
}


def scan(text: str) -> LegalDecision:
    findings: list[LegalFinding] = []
    for rule, compiled in _COMPILED:
        for pattern in compiled:
            match = pattern.search(text)
            if match:
                findings.append(
                    LegalFinding(
                        category=rule.category,
                        verdict=rule.verdict,
                        rule_id=rule.id,
                        reason=rule.reason,
                        matched=match.group(0),
                        human_line=rule.human_line,
                    )
                )
                break

    if not findings:
        return LegalDecision(
            verdict=LegalVerdict.ALLOW,
            findings=[],
            passport="Юридический риск не обнаружен детерминированным фильтром. Категория: none.",
        )

    verdict = max((f.verdict for f in findings), key=lambda v: _VERDICT_RANK[v])
    lines = [f"- {f.rule_id} ({f.category}): {f.reason} [совпало: «{f.matched}»]" for f in findings]
    return LegalDecision(verdict=verdict, findings=findings, passport="Паспорт проверки юриста\n" + "\n".join(lines))


def lawyer_reply(decision: LegalDecision) -> str:
    if decision.verdict == LegalVerdict.ALLOW:
        return "Юрист: юридических стоп-факторов нет. Можно продолжать в рамках полномочий."
    headline = {
        LegalVerdict.BLOCK: "Юрист: блок.",
        LegalVerdict.ESCALATE_HUMAN: "Юрист: только с человеком.",
        LegalVerdict.ALLOW_WITH_CONDITIONS: "Юрист: можно с оговорками.",
    }[decision.verdict]
    unique_lines: list[str] = []
    for finding in decision.findings:
        if finding.human_line not in unique_lines:
            unique_lines.append(finding.human_line)
    return headline + " " + " ".join(unique_lines)
