"""Section analytics: asks Claude for a short human-readable summary of one
section's current state plus a traffic-light status, based on the same
aggregated snapshot the "Сегодня" dashboard builds (app.dashboard.service) -
no parallel data-gathering logic.

Статус модели не может быть «зеленее» фактов (0084-c): до вызова LLM сервер
считает детерминированный пол статуса (`status_floor`, сейчас — для
производства по оценке готовности app/production/readiness.py). Если модель
вернула статус лучше пола, он заменяется полом с `status_floor_reason`. Без
ИИ (нет ключа, сбой API, нет `tool_use`) эндпоинт не падает: статус — пол,
`source="rules"`, текст из причин; у разделов без пола — `unknown`.
"""

import json
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.ai import cache as ai_cache
from app.ai.schemas import SectionAnalyticsOut
from app.core.config import settings
from app.dashboard.service import SECTION_BUILDERS, SECTION_LABELS
from app.users.models import User

log = logging.getLogger(__name__)

SUBMIT_TOOL_NAME = "submit_section_analysis"

SYSTEM_PROMPT = (
    "Ты — аналитик системы управления производством модульных домов «Soborbum». Тебе передан JSON "
    "с реальными агрегированными цифрами по одному разделу предприятия на текущий момент. Дай короткую "
    "аналитику по этому разделу.\n\n"
    "Правила:\n"
    "- summary — 2-3 предложения по-русски о текущем состоянии раздела: что происходит сейчас, что "
    "идёт хорошо, что требует внимания. Основывайся СТРОГО на переданных цифрах, не выдумывай факты "
    "и числа, которых нет во входных данных.\n"
    "- status — светофор по разделу: «red» — есть серьёзные проблемы, требующие немедленного "
    "вмешательства; «yellow» — есть моменты, которым нужно уделить внимание, или данных недостаточно "
    "для оценки; «green» — по переданным данным отклонений не найдено.\n"
    "- Недостаток данных — не благополучие. Если в данных есть состояние «insufficient_data» "
    "(«Недостаточно данных») или «needs_reconciliation» («Нужна сверка»), прямо скажи, каких данных "
    "не хватает, и не пиши, что всё в порядке. Если передан минимальный статус по правилам, "
    "статус не может быть лучше него.\n"
    "- Отвечай ТОЛЬКО вызовом инструмента submit_section_analysis, без текста."
)

TOOL_SCHEMA = {
    "name": SUBMIT_TOOL_NAME,
    "description": "Отправить готовую аналитику по разделу: краткое резюме и статус-светофор.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "2-3 предложения по-русски о текущем состоянии раздела.",
            },
            "status": {"type": "string", "enum": ["red", "yellow", "green"]},
        },
        "required": ["summary", "status"],
    },
}

# Чем меньше, тем хуже.
_STATUS_RANK = {"red": 0, "yellow": 1, "green": 2}

UNKNOWN_SUMMARY = "ИИ недоступен, оценка не выполнена."


class _NoAnalysis(Exception):
    """ИИ не дал аналитику: нет ключа, сбой API или ответ без tool_use."""


def status_floor(section: str, snapshot: dict) -> tuple[str | None, str | None, list[str]]:
    """Минимальный статус раздела по фактам: (статус, причина, тексты причин).
    None — у раздела пола нет, модель решает сама."""
    if section != "production":
        return None, None, []
    worst = snapshot.get("worst_state")
    reasons = [
        f"{r['production']}: {r['text']}" for r in snapshot.get("top_reasons") or []
    ]
    label = snapshot.get("worst_state_label")
    if worst in ("insufficient_data", "needs_reconciliation"):
        return "yellow", f"Материалы производства: {str(label).lower()}", reasons
    if worst == "shortfall":
        return "red", f"Материалы производства: {str(label).lower()}", reasons
    return None, None, reasons


def _ask_model(section: str, snapshot: dict, floor: str | None, floor_reason: str | None) -> dict:
    if not settings.llm_configured:
        raise _NoAnalysis("нет ключа активного ИИ-провайдера")
    from app.core.llm import llm_client

    content = (
        f"Данные раздела «{SECTION_LABELS.get(section, section)}» на "
        f"{datetime.now(timezone.utc).date().isoformat()}:\n\n"
        + json.dumps(snapshot, ensure_ascii=False, default=str)
    )
    if floor is not None:
        content += f"\n\nМинимальный статус по правилам: {floor} ({floor_reason})."
    try:
        response = llm_client().messages.create(
            model=settings.llm_model,
            max_tokens=768,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content}],
            tools=[TOOL_SCHEMA],
            tool_choice={"type": "tool", "name": SUBMIT_TOOL_NAME},
        )
    except Exception as error:  # noqa: BLE001 — сеть/квоты/5xx: ответ по правилам, не 500
        log.warning("аналитика раздела %s: ИИ недоступен: %s", section, error)
        raise _NoAnalysis(str(error)) from error

    tool_use = next((block for block in response.content if block.type == "tool_use"), None)
    if tool_use is None:
        raise _NoAnalysis("ИИ не вернул аналитику")
    return tool_use.input


def _rules_result(section: str, floor: str | None, floor_reason: str | None, reasons: list[str]) -> SectionAnalyticsOut:
    if floor is None:
        return SectionAnalyticsOut(
            section=section, generated_at=datetime.now(timezone.utc),
            summary=UNKNOWN_SUMMARY, status="unknown", source="rules",
        )
    summary = f"ИИ недоступен, оценка по правилам. {floor_reason}."
    if reasons:
        summary += " " + "; ".join(reasons[:3]) + "."
    return SectionAnalyticsOut(
        section=section, generated_at=datetime.now(timezone.utc),
        summary=summary, status=floor, source="rules",
    )


def generate_section_analytics(db: Session, user: User, section: str, force: bool = False) -> SectionAnalyticsOut:
    cache_key = f"section_analytics:{section}"
    cached = ai_cache.get(db, cache_key, force)
    if cached is not None:
        return SectionAnalyticsOut(**cached)

    _, builder = SECTION_BUILDERS[section]
    snapshot = builder(db)
    floor, floor_reason, reasons = status_floor(section, snapshot)

    try:
        payload = _ask_model(section, snapshot, floor, floor_reason)
    except _NoAnalysis:
        # Не кешируем: как только ИИ снова доступен, следующий запрос должен
        # спросить его, а не ждать TTL.
        return _rules_result(section, floor, floor_reason, reasons)

    raw_status = payload.get("status")
    section_status = raw_status if raw_status in _STATUS_RANK else "yellow"
    applied_floor_reason = None
    if floor is not None and _STATUS_RANK[section_status] > _STATUS_RANK[floor]:
        section_status = floor
        applied_floor_reason = floor_reason

    result = SectionAnalyticsOut(
        section=section,
        generated_at=datetime.now(timezone.utc),
        summary=payload.get("summary", ""),
        status=section_status,
        source="ai",
        status_floor_reason=applied_floor_reason,
    )
    ai_cache.set(db, cache_key, result.model_dump(mode="json"), result.generated_at)
    return result
