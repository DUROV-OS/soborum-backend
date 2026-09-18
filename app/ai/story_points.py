"""Скрытая от сотрудников оценка объёма задачи ИИ (0070-d). Вызывается
best-effort из app.tasks.service.create_task и из разового backfill
(app.tasks.story_points_backfill) - при любой ошибке (нет ключа, сеть,
таймаут, парсинг) молча деградирует до None, никогда не роняет создание
задачи. Значение существует только в БД (Task.story_points) и никогда не
отдаётся через API.
"""

from app.core.config import settings

SUBMIT_TOOL_NAME = "submit_story_points"

SCALE = (1, 2, 3, 5, 8)

SYSTEM_PROMPT = (
    "Ты оцениваешь объём («размер») задачи в системе управления производством модульных домов "
    "«Soborbum» по фиксированной шкале сторипоинтов: 1 — тривиальная быстрая правка (несколько "
    "минут-часов), 2 — небольшая задача в пределах дня, 3 — задача на день-два, 5 — заметная "
    "многодневная работа, 8 — крупная задача на неделю и больше.\n\n"
    "Правила:\n"
    "- Оценивай только по переданным названию, описанию и разделу-источнику задачи, не выдумывай "
    "детали, которых там нет.\n"
    "- Ответ — ровно одно число из шкалы {1, 2, 3, 5, 8}.\n"
    "- Отвечай ТОЛЬКО вызовом инструмента submit_story_points, без текста."
)

TOOL_SCHEMA = {
    "name": SUBMIT_TOOL_NAME,
    "description": "Отправить оценку объёма задачи по шкале сторипоинтов.",
    "input_schema": {
        "type": "object",
        "properties": {
            "story_points": {"type": "integer", "enum": list(SCALE)},
        },
        "required": ["story_points"],
    },
}


def estimate_story_points(title: str, description: str | None, section_hint: str) -> int | None:
    """Оценить объём задачи по шкале {1,2,3,5,8}. Возвращает None при любой
    ошибке или недоступности ИИ - вызывающий код (create_task, backfill)
    просто оставляет story_points пустым."""
    if not settings.anthropic_api_key:
        return None

    try:
        from app.core.llm import anthropic_client

        client = anthropic_client()
        response = client.messages.create(
            model=settings.ai_model,
            max_tokens=64,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": f"Раздел-источник: {section_hint}\nНазвание: {title}\n"
                    f"Описание: {description or '(нет описания)'}",
                }
            ],
            tools=[TOOL_SCHEMA],
            tool_choice={"type": "tool", "name": SUBMIT_TOOL_NAME},
        )
    except Exception:  # noqa: BLE001 — сеть/квоты/ключ: молча деградируем, задача создаётся без оценки
        return None

    tool_use = next((block for block in response.content if block.type == "tool_use"), None)
    if tool_use is None:
        return None

    points = tool_use.input.get("story_points")
    return points if points in SCALE else None
