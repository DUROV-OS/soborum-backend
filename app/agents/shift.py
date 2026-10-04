"""Company shift: all eight roles, cross-review, Human Approval queue.

Not a chatbot. Claude is optional and may only cite SharedContext.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.agents.context import gather
from app.agents.ids import CROSS_REVIEWERS, DAILY_QUESTIONS, DOES_NOT_OWN, RU_LABELS, AgentId
from app.agents.legal import scan
from app.agents.runtime import _live_hit, _rank_hits
from app.agents.types import LegalDecision, LegalVerdict, SharedContext
from app.core.config import settings

log = logging.getLogger("app.agents.shift")

# Откуда взялась позиция роли. «live» — выдержка из живого среза базы DurovOS;
# «llm_without_facts» — Claude ответил, но живого хита у роли не было, это не
# факт из системы; «none» — ни того, ни другого, позиция — честное «нет данных».
STANCE_LIVE = "live"
STANCE_LLM_WITHOUT_FACTS = "llm_without_facts"
STANCE_NONE = "none"


# Статус кросс-проверки. Реальная проверка сейчас есть только у юриста
# (детерминированный фильтр стоп-факторов); остальные роли своего чек-листа
# пока не имеют (P1, #37 п.2) и честно пишут «Не проверено».
REVIEW_CHECKED_OK = "checked_ok"
REVIEW_CHECKED_ESCALATE = "checked_escalate"
REVIEW_NOT_CHECKED = "not_checked"


@dataclass
class ShiftReview:
    reviewer: AgentId
    text: str
    escalate: bool
    kind: str = "ops"
    status: str = REVIEW_NOT_CHECKED


@dataclass
class ShiftItemDraft:
    agent: AgentId
    daily_question: str
    stance: str
    citations: list[str]
    legal_verdict: str
    has_live_data: bool = False
    stance_source: str = STANCE_NONE
    reviews: list[ShiftReview] = field(default_factory=list)


@dataclass
class ApprovalDraft:
    kind: str
    title: str
    detail: str
    agent: AgentId


@dataclass
class ShiftDraft:
    items: list[ShiftItemDraft]
    approvals: list[ApprovalDraft]
    verdict: str
    summary: str
    claude_used: bool


def run_shift(db: Session | None = None, vault_root: str | None = None) -> ShiftDraft:
    claude_used = False
    items: list[ShiftItemDraft] = []
    for agent_id in AgentId:
        question = DAILY_QUESTIONS[agent_id]
        legal = scan(question)
        context = gather(question, [agent_id], vault_root, db)
        relevant = _rank_hits(agent_id, context.hits)
        live = [hit for hit in relevant if _live_hit(agent_id, hit)]
        citations = [hit.title for hit in (live or relevant)[:3] if hit.title]
        stance: str | None = None
        # has_live_data = в позиции использован живой хит, а не «Claude ответил».
        has_live_data = False
        stance_source = STANCE_NONE
        if live:
            stance = " ".join(hit.excerpt for hit in live[:2] if hit.excerpt).strip() or None
            if stance:
                has_live_data = True
                stance_source = STANCE_LIVE
        if not stance:
            claude_stance = _claude_stance(agent_id, question, context)
            if claude_stance:
                claude_used = True
                stance = claude_stance
                stance_source = STANCE_LLM_WITHOUT_FACTS
        if not stance:
            # No live source, no Claude — do not fabricate. Mark honestly.
            stance = _no_data_stance(agent_id)
        items.append(
            ShiftItemDraft(
                agent=agent_id,
                daily_question=question,
                stance=stance,
                citations=citations or [],
                legal_verdict=legal.verdict.value,
                has_live_data=has_live_data,
                stance_source=stance_source,
            )
        )

    by_id = {item.agent: item for item in items}
    for item in items:
        for reviewer in CROSS_REVIEWERS[item.agent]:
            item.reviews.append(_review(reviewer, item, by_id))

    approvals = _approvals(items)
    verdict = _verdict(items, approvals)
    summary = _summary(items, approvals, verdict, claude_used)
    return ShiftDraft(
        items=items,
        approvals=approvals,
        verdict=verdict,
        summary=summary,
        claude_used=claude_used,
    )


def _review(reviewer: AgentId, item: ShiftItemDraft, by_id: dict[AgentId, ShiftItemDraft]) -> ShiftReview:
    if reviewer == AgentId.LAWYER:
        legal = scan(item.daily_question + "\n" + _stance_for_legal(item.stance))
        escalate = legal.verdict != LegalVerdict.ALLOW
        return ShiftReview(
            reviewer=reviewer,
            kind="legal",
            escalate=escalate,
            status=REVIEW_CHECKED_ESCALATE if escalate else REVIEW_CHECKED_OK,
            text=(
                _legal_reviewer_line(legal)
                if escalate
                else "Юрист черновик посмотрел: детерминированный фильтр стоп-факторов не нашёл."
            ),
        )
    # Проверки у роли нет — не выдаём отсутствие проверки за её результат.
    return ShiftReview(
        reviewer=reviewer,
        kind="ops",
        escalate=False,
        status=REVIEW_NOT_CHECKED,
        text=f"Не проверено: у {RU_LABELS[reviewer]}а нет проверки по этому вопросу.",
    )


LEGAL_QUEUE_LINE = "В очередь: без вашего «да» не выпускаем."


def _legal_reviewer_line(legal: LegalDecision) -> str:
    lines = list(dict.fromkeys(finding.human_line for finding in legal.findings))
    tail = " " + " ".join(lines) if lines else ""
    return LEGAL_QUEUE_LINE + tail


def _approvals(items: list[ShiftItemDraft]) -> list[ApprovalDraft]:
    approvals: list[ApprovalDraft] = []
    seen: set[tuple[str, AgentId]] = set()
    for item in items:
        if item.legal_verdict != LegalVerdict.ALLOW:
            kind = "legal" if item.legal_verdict == LegalVerdict.BLOCK else "pricing"
            detail = (
                f"{RU_LABELS[item.agent].capitalize()} принёс вопрос, который нельзя выпускать самим: "
                f"«{item.daily_question}»"
            )
            _add_approval(
                approvals,
                seen,
                ApprovalDraft(
                    kind=kind,
                    title=_approval_title(item.agent, item.daily_question),
                    detail=detail,
                    agent=item.agent,
                ),
            )
        for review in item.reviews:
            if not review.escalate:
                continue
            _add_approval(
                approvals,
                seen,
                ApprovalDraft(
                    kind=review.kind,
                    title=_approval_title(item.agent, review.text),
                    detail=review.text,
                    agent=item.agent,
                ),
            )
    return approvals


def _add_approval(
    approvals: list[ApprovalDraft],
    seen: set[tuple[str, AgentId]],
    draft: ApprovalDraft,
) -> None:
    # Одна роль = один пункт смены, поэтому (kind, agent) — это (kind, item_id):
    # разные вопросы одного типа от разных ролей больше не схлопываются в один
    # по одинаковому заголовку.
    key = (draft.kind, draft.agent)
    if key in seen:
        return
    seen.add(key)
    approvals.append(draft)


def _verdict(items: list[ShiftItemDraft], approvals: list[ApprovalDraft]) -> str:
    if any(item.legal_verdict == LegalVerdict.BLOCK for item in items):
        return LegalVerdict.BLOCK
    if approvals or any(item.legal_verdict != LegalVerdict.ALLOW for item in items):
        return LegalVerdict.ESCALATE_HUMAN
    return LegalVerdict.ALLOW


def _summary(
    items: list[ShiftItemDraft],
    approvals: list[ApprovalDraft],
    verdict: str,
    claude_used: bool,
) -> str:
    unchecked_items = sum(1 for item in items if not any(_is_checked(review) for review in item.reviews))
    not_checked = sum(1 for item in items for review in item.reviews if review.status == REVIEW_NOT_CHECKED)
    without_data = sum(1 for item in items if not item.has_live_data)
    if approvals:
        head = f"Вам решить {len(approvals)} {_plural(len(approvals), 'вопрос', 'вопроса', 'вопросов')}."
    elif unchecked_items:
        # Без реальной проверки «ничего не нужно» — только с оговоркой.
        head = (
            f"Сейчас от вас ничего не нужно, но {unchecked_items} "
            f"{_plural(unchecked_items, 'пункт не проверен', 'пункта не проверены', 'пунктов не проверены')}."
        )
    else:
        head = "Сейчас от вас ничего не нужно."
    notes = []
    if without_data:
        notes.append(
            f"{without_data} {_plural(without_data, 'пункт', 'пункта', 'пунктов')} без данных из системы"
        )
    if not_checked:
        notes.append(f"проверок «Не проверено»: {not_checked}")
    if not notes:
        return head
    tail = "; ".join(notes)
    return f"{head} {tail[0].upper()}{tail[1:]}."


def _is_checked(review: ShiftReview) -> bool:
    return review.status in (REVIEW_CHECKED_OK, REVIEW_CHECKED_ESCALATE)


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


APPROVAL_TITLE_BRIEF = 80


def _approval_title(agent: AgentId, detail: str) -> str:
    """Заголовок согласования — чей пункт и о чём он, а не общее «Нужно ваше решение»."""
    brief = " ".join(detail.split())
    # Общая строка очереди юриста одинакова у всех — суть в найденных стоп-факторах.
    if brief.startswith(LEGAL_QUEUE_LINE) and brief != LEGAL_QUEUE_LINE:
        brief = brief[len(LEGAL_QUEUE_LINE):].strip()
    if len(brief) > APPROVAL_TITLE_BRIEF:
        cut = brief[:APPROVAL_TITLE_BRIEF].rsplit(" ", 1)[0] or brief[:APPROVAL_TITLE_BRIEF]
        brief = cut.rstrip(" ,.;:—-") + "…"
    return f"{RU_LABELS[agent].capitalize()}: {brief}"


def _no_data_stance(agent_id: AgentId) -> str:
    return (
        "Нет данных: в базе DurovOS живого факта по роли нет и ИИ недоступен — "
        "ничего не выдумываю."
    )


def _stance_for_legal(stance: str) -> str:
    """Do not treat passport disclaimers or vault excerpts as a commercial request."""
    text = stance.split("Не моё:", 1)[0]
    if "Опираюсь на:" in text:
        text = text.split("Опираюсь на:", 1)[0]
    return text.strip()


def _claude_stance(agent_id: AgentId, question: str, context: SharedContext) -> str | None:
    if not settings.llm_configured:
        return None
    ranked = _rank_hits(agent_id, context.hits)
    pack = [f"- {hit.title} ({hit.path}): {hit.excerpt}" for hit in ranked if hit.excerpt][:8]
    if not pack:
        pack = [
            "- (фактов из базы DurovOS и vault в контексте нет — не выдумывай цифры)"
        ]
    try:
        from app.core.llm import llm_client

        client = llm_client(timeout=45.0, max_retries=0)
        response = client.messages.create(
            model=settings.llm_model,
            max_tokens=160,
            system=(
                f"Ты {RU_LABELS[agent_id]} Durov.House. Не чат-бот. "
                f"Не твоё: {DOES_NOT_OWN[agent_id]}. "
                "Ответь ровно 1–2 короткими предложениями. Без списков и без просьб прислать ещё данные. "
                "Если в цитатах есть срез базы DurovOS — назови 1–2 живых числа оттуда. "
                "Нет живого факта — одно предложение: факта нет, цифры не выдумываю. "
                "Не обещай цену, срок, договор, найм."
            ),
            messages=[
                {
                    "role": "user",
                    "content": f"Вопрос смены: {question}\n\nЦИТАТЫ:\n" + "\n".join(pack),
                }
            ],
        )
    except Exception as error:
        log.warning("ИИ не ответил за %s: %s", agent_id, error)
        return None
    chunks = [block.text for block in response.content if getattr(block, "type", "") == "text"]
    text = _clamp_sentences("\n".join(chunks).strip())
    return text or None


def _clamp_sentences(text: str, limit: int = 2) -> str:
    parts = [part.strip() for part in re.findall(r"[^.!?…]+[.!?…]?", text, flags=re.S) if part.strip()]
    return " ".join(parts[:limit]) or text.strip()
