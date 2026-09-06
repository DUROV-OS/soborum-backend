"""Section analytics: asks Claude for a short human-readable summary of one
section's current state plus a traffic-light status, based on the same
aggregated snapshot the "Сегодня" dashboard builds (app.dashboard.service) -
no parallel data-gathering logic.
"""

import json
from datetime import datetime, timezone

import anthropic
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.ai import cache as ai_cache
from app.ai import model_profiles
from app.ai.schemas import SectionAnalyticsOut
from app.core.config import settings
from app.dashboard.service import SECTION_BUILDERS, SECTION_LABELS
from app.users.models import User

SUBMIT_TOOL_NAME = "submit_section_analysis"

SYSTEM_PROMPT = (
    "Ты — аналитик системы управления производством модульных домов «Soborbum». Тебе передан JSON "
    "с реальными агрегированными цифрами по одному разделу предприятия на текущий момент. Дай короткую "
    "аналитику по этому разделу.\n\n"
    "Правила:\n"
    "- summary — 2-3 предложения по-русски о текущем состоянии раздела: что происходит сейчас, что "
    "идёт хорошо, что требует внимания. Основывайся СТРОГО на переданных цифрах, не выдумывай факты "
    "и числа, которых нет во входных данных.\n"
    "- status — светофор по разделу: «red» — дела совсем плохи, есть серьёзные проблемы, требующие "
    "немедленного вмешательства; «yellow» — в целом приемлемо, но есть моменты, которым нужно "
    "уделить больше внимания; «green» — всё в порядке, серьёзных проблем нет.\n"
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


def _get_client() -> anthropic.Anthropic:
    if not settings.anthropic_api_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="ИИ не настроен: не задан ANTHROPIC_API_KEY (см. backend/.env)",
        )
    return anthropic.Anthropic(api_key=settings.anthropic_api_key)


def generate_section_analytics(db: Session, user: User, section: str, force: bool = False) -> SectionAnalyticsOut:
    cache_key = f"section_analytics:{section}"
    cached = ai_cache.get(db, cache_key, force)
    if cached is not None:
        return SectionAnalyticsOut(**cached)

    _, builder = SECTION_BUILDERS[section]
    snapshot = builder(db)

    client = _get_client()
    response = client.messages.create(
        **model_profiles.profile_params(model_profiles.ModelProfile.QUICK),
        max_tokens=768,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": f"Данные раздела «{SECTION_LABELS.get(section, section)}» на "
                f"{datetime.now(timezone.utc).date().isoformat()}:\n\n"
                + json.dumps(snapshot, ensure_ascii=False, default=str),
            }
        ],
        tools=[TOOL_SCHEMA],
        tool_choice={"type": "tool", "name": SUBMIT_TOOL_NAME},
    )

    tool_use = next((block for block in response.content if block.type == "tool_use"), None)
    if tool_use is None:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="ИИ не вернул аналитику")

    payload = tool_use.input
    raw_status = payload.get("status")
    section_status = raw_status if raw_status in ("red", "yellow", "green") else "yellow"

    result = SectionAnalyticsOut(
        section=section,
        generated_at=datetime.now(timezone.utc),
        summary=payload.get("summary", ""),
        status=section_status,
    )
    ai_cache.set(db, cache_key, result.model_dump(mode="json"), result.generated_at)
    return result
