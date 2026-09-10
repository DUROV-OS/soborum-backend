"""«Спросить Марину о совещании» — одноразовый вопрос по конкретному
совещанию. Контекст = транскрипт этого совещания; плюс, если коннектор
базы знаний настроен и спрашивает администратор, Марине доступны read-only
инструменты базы знаний и веб-поиск (та же обвязка, что в app/ai/engine).

Голосовой триггер «Марина» и озвучка ответа — отдельная задача 0004-d, здесь
только текстовый вопрос и ответ на экран.
"""

from __future__ import annotations

import anthropic
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.ai import engine
from app.ai.meetings import transcript_lines
from app.ai.models import Meeting
from app.core.config import settings
from app.users.models import User

MAX_ROUNDS = 5

SYSTEM_PROMPT = (
    "Ты — Марина, ассистент системы управления производством модульных домов. "
    "Пользователь открыл конкретное совещание и задаёт по нему вопрос. Ниже — "
    "транскрипт этого совещания. Отвечай строго по существу вопроса, опираясь на "
    "транскрипт. Если доступны инструменты базы знаний компании (их имена "
    "начинаются с «knowledge-base_») и вопрос требует справки по проекту, "
    "техкарте, поставщику или правилам — сверься с базой знаний и учти найденное. "
    "Не выдумывай фактов, которых нет ни в транскрипте, ни в базе знаний; если "
    "данных не хватает — так и скажи. Пиши по-русски, кратко и по делу, можно "
    "Markdown."
)


def _transcript_block(db: Session, meeting: Meeting) -> str:
    lines = transcript_lines(db, meeting)
    if not lines:
        return "(транскрипт совещания пуст)"
    return "\n".join(f"[{line.speaker}] {line.text}" for line in lines)


def answer_meeting_question(db: Session, user: User, meeting: Meeting, question: str) -> str:
    if not settings.anthropic_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Марина не подключена: не задан ANTHROPIC_API_KEY.",
        )
    text = (question or "").strip()
    if not text:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Пустой вопрос")

    title = meeting.title or f"Совещание #{meeting.id}"
    user_block = (
        f"Совещание: «{title}» (начато {meeting.started_at:%Y-%m-%d %H:%M}).\n\n"
        f"Транскрипт:\n{_transcript_block(db, meeting)}\n\n---\nВопрос: {text}"
    )
    messages: list[dict] = [{"role": "user", "content": user_block}]
    client = engine._get_client()

    for _ in range(MAX_ROUNDS):
        kwargs, mcp_servers = engine._build_request(db, SYSTEM_PROMPT, messages, [], user)
        try:
            if mcp_servers is not None:
                response = client.beta.messages.create(
                    betas=[engine.MCP_BETA], mcp_servers=mcp_servers, **kwargs
                )
            else:
                response = client.messages.create(**kwargs)
        except anthropic.APIError:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Марина временно недоступна. Попробуйте позже.",
            ) from None

        blocks = [block.model_dump(mode="json") for block in response.content]
        if response.stop_reason == "pause_turn":
            # Провайдерский инструмент (веб / база знаний) в процессе — дослать
            # накопленное и продолжить, как это делает engine._advance.
            messages.append({"role": "assistant", "content": blocks})
            continue

        answer = "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
        return answer or "Не удалось сформулировать ответ по совещанию."

    return "Не удалось получить ответ за отведённое число шагов. Попробуйте переформулировать вопрос."
