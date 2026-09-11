"""«Спросить Марину о совещании» — одноразовый вопрос по конкретному
совещанию.

Идёт через общий движок ассистента (app/ai/engine) в режиме NO_ACTIONS: у
Марины те же инструменты чтения, что в разделе «Марина» — живой срез базы
DurovOS, read-only инструменты разделов, база знаний (для админа) и веб.
Никаких изменений данных (NO_ACTIONS отфильтровывает всё, кроме чтения).
Контекст вопроса — транскрипт этого совещания.

Голосовой ответ: движок возвращает первую строку «Голосом: …» (короткая
фраза для озвучки) + развёрнутый текст ниже — фронт разбивает их сам.
"""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.ai import engine
from app.ai.meeting_notes import meeting_context_line
from app.ai.meetings import transcript_lines
from app.ai.models import Chat, ChatDomain, ChatMode, Meeting
from app.core.config import settings
from app.users.models import User


def _transcript_block(db: Session, meeting: Meeting) -> str:
    lines = [ln for ln in transcript_lines(db, meeting) if not ln.is_assistant_query]
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
    message = (
        f"Вопрос задан во время совещания «{title}». Ниже транскрипт этого совещания — "
        "это контекст разговора. Разделяй источники по общему правилу: вопрос про "
        "текущее состояние и процессы компании (клиенты, заказы, склад, производство, "
        "монтаж, задачи, деньги, сроки) — смотри в базе DurovOS через инструменты "
        "чтения, а не в транскрипте и не в базе знаний; справочные и общие вопросы или "
        "конкретика о том, чего в системе нет — база знаний; публичные факты — интернет.\n\n"
        f"{meeting_context_line(meeting)}"
        f"ТРАНСКРИПТ СОВЕЩАНИЯ:\n{_transcript_block(db, meeting)}\n\n---\nВОПРОС: {text}"
    )

    # Временный «чат» только ради прогона движка; удаляем вместе с сообщениями.
    chat = Chat(owner_id=user.id, domain=ChatDomain.GENERAL, mode=ChatMode.NO_ACTIONS)
    db.add(chat)
    db.commit()
    db.refresh(chat)
    try:
        result = engine.run_turn(db, chat, user, message, voice_lead=True)
    finally:
        db.delete(chat)
        db.commit()

    return (result.reply or "").strip() or "Не удалось сформулировать ответ по совещанию."
