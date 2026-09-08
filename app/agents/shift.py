"""Company shift: all eight roles, cross-review, Human Approval queue.

Not a chatbot. Claude is optional and may only cite SharedContext.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from app.agents.connectors import live_stance_for
from app.agents.context import gather
from app.agents.ids import CROSS_REVIEWERS, DAILY_QUESTIONS, DOES_NOT_OWN, RU_LABELS, AgentId
from app.agents.legal import scan
from app.agents.runtime import _live_hit, _rank_hits
from app.agents.types import LegalVerdict, SharedContext
from app.core.config import settings

log = logging.getLogger("app.agents.shift")


@dataclass
class ShiftReview:
    reviewer: AgentId
    text: str
    escalate: bool
    kind: str = "ops"


@dataclass
class ShiftItemDraft:
    agent: AgentId
    daily_question: str
    stance: str
    citations: list[str]
    legal_verdict: str
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


def run_shift(vault_root: str | None = None) -> ShiftDraft:
    claude_used = False
    items: list[ShiftItemDraft] = []
    for agent_id in AgentId:
        question = DAILY_QUESTIONS[agent_id]
        legal = scan(question)
        context = gather(question, [agent_id], vault_root)
        relevant = _rank_hits(agent_id, context.hits)
        live = [hit for hit in relevant if _live_hit(agent_id, hit)]
        citations = [hit.title for hit in (live or relevant)[:3] if hit.title]
        stance: str | None = None
        if live:
            stance = live_stance_for(agent_id)
        if not stance:
            claude_stance = _claude_stance(agent_id, question, context)
            if claude_stance:
                claude_used = True
                stance = claude_stance
        if not stance:
            stance = _plain_stance(agent_id, relevant)
        items.append(
            ShiftItemDraft(
                agent=agent_id,
                daily_question=question,
                stance=stance,
                citations=citations or [],
                legal_verdict=legal.verdict.value,
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
    if reviewer == AgentId.FINANCE and item.agent == AgentId.SALES:
        return ShiftReview(
            reviewer=reviewer,
            kind="pricing",
            escalate=True,
            text="Нельзя самому ставить окончательную цену или скидку больше 5%. Это решает человек.",
        )
    if reviewer == AgentId.PRODUCTION and item.agent == AgentId.SALES:
        return ShiftReview(
            reviewer=reviewer,
            kind="ops",
            escalate=False,
            text="Срок клиенту не обещать, пока цех не подтвердил загрузку и материал.",
        )
    if reviewer == AgentId.SALES and item.agent == AgentId.FINANCE:
        return ShiftReview(
            reviewer=reviewer,
            kind="ops",
            escalate=False,
            text="Цену и срок из CRM в договор сам не переписываю.",
        )
    if reviewer == AgentId.WAREHOUSE and item.agent == AgentId.PRODUCTION:
        return ShiftReview(
            reviewer=reviewer,
            kind="ops",
            escalate=False,
            text="Заявка цеха без остатка в МойСкладе — не обещание поставки.",
        )
    if reviewer == AgentId.ENGINEER and item.agent == AgentId.PRODUCTION:
        return ShiftReview(
            reviewer=reviewer,
            kind="ops",
            escalate=False,
            text="Отклонение от техкарты на площадку не выпускать. Нестандарт — не типовой узел.",
        )
    if reviewer == AgentId.LAWYER:
        legal = scan(item.daily_question + "\n" + _stance_for_legal(item.stance))
        escalate = legal.verdict != LegalVerdict.ALLOW
        return ShiftReview(
            reviewer=reviewer,
            kind="legal",
            escalate=escalate,
            text=(
                "В очередь: без вашего «да» не выпускаем."
                if escalate
                else "Стоп-факторов нет. Ворованную базу конкурента нельзя."
            ),
        )
    return ShiftReview(
        reviewer=reviewer,
        kind="ops",
        escalate=False,
        text=f"{RU_LABELS[reviewer].capitalize()} видел черновик и своего стоп-фактора не нашёл.",
    )


def _approvals(items: list[ShiftItemDraft]) -> list[ApprovalDraft]:
    approvals: list[ApprovalDraft] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        if item.legal_verdict != LegalVerdict.ALLOW:
            kind = "legal" if item.legal_verdict == LegalVerdict.BLOCK else "pricing"
            _add_approval(
                approvals,
                seen,
                ApprovalDraft(
                    kind=kind,
                    title="Нужно ваше решение",
                    detail=f"{RU_LABELS[item.agent].capitalize()} принёс вопрос, который нельзя выпускать самим.",
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
                    title=_approval_title(review.kind),
                    detail=review.text,
                    agent=item.agent,
                ),
            )
    return approvals


def _add_approval(
    approvals: list[ApprovalDraft],
    seen: set[tuple[str, str]],
    draft: ApprovalDraft,
) -> None:
    key = (draft.kind, draft.title)
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
    if approvals:
        return f"Вам решить {len(approvals)} {_plural_questions(len(approvals))}."
    return "Сейчас от вас ничего не нужно."


def _plural_questions(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "вопрос"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "вопроса"
    return "вопросов"


def _approval_title(kind: str) -> str:
    if kind == "pricing":
        return "Цена и скидка — только вы"
    if kind == "legal":
        return "Юрист просит вас посмотреть"
    return "Нужно ваше решение"


def _plain_stance(agent_id: AgentId, hits) -> str:
    crm = [
        hit.title.removeprefix("amoCRM: ")
        for hit in hits
        if hit.source == "crm" and (hit.path or "").startswith("amocrm/leads/")
    ]
    stock = [
        hit.title.removeprefix("МойСклад: ").removeprefix("МойСклад заказ: ")
        for hit in hits
        if hit.source == "warehouse" and (hit.path or "").startswith("moysklad/")
    ]
    if agent_id == AgentId.LAWYER:
        return "Ворованную базу конкурента нельзя. Договор и обещание от имени владельца — вам."
    bits: list[str] = []
    if crm and agent_id in {AgentId.COORDINATOR, AgentId.SALES, AgentId.MARKETER, AgentId.FINANCE}:
        bits.append(f"В amoCRM открыты: {', '.join(crm[:2])}.")
    if stock and agent_id in {AgentId.COORDINATOR, AgentId.WAREHOUSE, AgentId.PRODUCTION, AgentId.FINANCE}:
        bits.append(f"В МойСкладе: {', '.join(stock[:2])}.")
    if not bits:
        return "Живого факта нет — сделки и остатки не выдумываю."
    return " ".join(bits[:2])


def _stance_for_legal(stance: str) -> str:
    """Do not treat passport disclaimers or vault excerpts as a commercial request."""
    text = stance.split("Не моё:", 1)[0]
    if "Опираюсь на:" in text:
        text = text.split("Опираюсь на:", 1)[0]
    return text.strip()


def _claude_stance(agent_id: AgentId, question: str, context: SharedContext) -> str | None:
    if not settings.anthropic_api_key:
        return None
    ranked = _rank_hits(agent_id, context.hits)
    pack = [f"- {hit.title} ({hit.path}): {hit.excerpt}" for hit in ranked if hit.excerpt][:8]
    if not pack:
        pack = [
            "- (цитат vault/CRM/склада нет: живых фактов в контексте нет — не выдумывай цифры)"
        ]
    try:
        from app.core.llm import anthropic_client

        client = anthropic_client(timeout=45.0, max_retries=0)
        response = client.messages.create(
            model=settings.ai_model,
            max_tokens=160,
            system=(
                f"Ты {RU_LABELS[agent_id]} Durov.House. Не чат-бот. "
                f"Не твоё: {DOES_NOT_OWN[agent_id]}. "
                "Ответь ровно 1–2 короткими предложениями. Без списков и без просьб прислать ещё данные. "
                "Если в цитатах есть amoCRM или МойСклад — назови 1–2 живых факта оттуда. "
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
        log.warning("Claude не ответил за %s: %s", agent_id, error)
        return None
    chunks = [block.text for block in response.content if getattr(block, "type", "") == "text"]
    text = _clamp_sentences("\n".join(chunks).strip())
    return text or None


def _clamp_sentences(text: str, limit: int = 2) -> str:
    parts = [part.strip() for part in re.findall(r"[^.!?…]+[.!?…]?", text, flags=re.S) if part.strip()]
    return " ".join(parts[:limit]) or text.strip()
