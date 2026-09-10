"""Заметки Марины по совещанию: резюме, решения, задачи, открытые вопросы.

Пересчитываются целиком по текущему транскрипту (не дифф), чтобы не
расходились: инкрементально по мере роста транскрипта (порог NEW_LINES_THRESHOLD)
и один раз на finish. Плюс сборка итогового markdown-документа для базы знаний.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from datetime import datetime, timezone

import anthropic
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.ai.meetings import transcript_lines
from app.ai.models import Meeting, MeetingNotes
from app.core.config import settings

NEW_LINES_THRESHOLD = 8

SUBMIT_TOOL_NAME = "submit_meeting_notes"

SYSTEM_PROMPT = (
    "Ты — Марина, ассистент системы управления производством модульных домов. Тебе "
    "передан транскрипт совещания по репликам. Составь по нему структурированные "
    "заметки на русском.\n\n"
    "Правила:\n"
    "- title — короткое название совещания (3-7 слов), по сути разговора, без "
    "кавычек и слова «Совещание».\n"
    "- summary — 2-4 предложения: о чём совещание и главное по итогу.\n"
    "- decisions — принятые решения, по одному пункту на решение; пусто, если явных "
    "решений нет.\n"
    "- tasks — поручения: что сделать и, если названо, кто и к какому сроку.\n"
    "- questions — открытые вопросы, что осталось нерешённым.\n"
    "- Опирайся строго на транскрипт, ничего не выдумывай. Если данных мало — "
    "делай короткие заметки или пустые списки.\n"
    "- Отвечай ТОЛЬКО вызовом инструмента submit_meeting_notes, без текста."
)

_STR_LIST = {"type": "array", "items": {"type": "string"}}

TOOL_SCHEMA = {
    "name": SUBMIT_TOOL_NAME,
    "description": "Отправить готовые заметки по совещанию.",
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "summary": {"type": "string"},
            "decisions": _STR_LIST,
            "tasks": _STR_LIST,
            "questions": _STR_LIST,
        },
        "required": ["title", "summary", "decisions", "tasks", "questions"],
    },
}

_locks: dict[int, threading.Lock] = defaultdict(threading.Lock)


def notes_ai_enabled() -> bool:
    return bool(settings.anthropic_api_key)


def _clean_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _content_lines(lines):
    """Обычные реплики — без обращений к Марине по слову-триггеру."""
    return [line for line in lines if not line.is_assistant_query]


def _transcript_text(lines) -> str:
    return "\n".join(f"[{line.speaker}] {line.text}" for line in lines)


def _generate(transcript_text: str) -> dict:
    from app.core.llm import anthropic_client

    client = anthropic_client(timeout=60.0, max_retries=2)
    response = client.messages.create(
        model=settings.ai_model,
        max_tokens=1536,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"Транскрипт совещания:\n\n{transcript_text}"}],
        tools=[TOOL_SCHEMA],
        tool_choice={"type": "tool", "name": SUBMIT_TOOL_NAME},
    )
    tool_use = next((b for b in response.content if b.type == "tool_use"), None)
    if tool_use is None:
        raise RuntimeError("no tool_use in notes response")
    payload = tool_use.input or {}
    return {
        "title": str(payload.get("title", "")).strip().strip("«»\"'")[:255],
        "summary": str(payload.get("summary", "")).strip(),
        "decisions": _clean_list(payload.get("decisions")),
        "tasks": _clean_list(payload.get("tasks")),
        "questions": _clean_list(payload.get("questions")),
    }


def _empty_notes(db: Session, meeting: Meeting) -> MeetingNotes:
    notes = MeetingNotes(
        meeting_id=meeting.id, summary="", decisions=[], tasks=[], questions=[], source_line_count=0
    )
    db.add(notes)
    db.commit()
    db.refresh(notes)
    return notes


def refresh_notes(db: Session, meeting: Meeting, *, force: bool = False) -> tuple[MeetingNotes, bool]:
    """Возвращает (notes, stale). stale=True — последний вызов модели не удался и
    отдаётся прошлая версия. Требует настроенного ANTHROPIC_API_KEY."""
    if not notes_ai_enabled():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"detail": "ИИ-заметки отключены: не задан ANTHROPIC_API_KEY", "ai_enabled": False},
        )

    with _locks[meeting.id]:
        lines = _content_lines(transcript_lines(db, meeting))
        count = len(lines)
        existing = db.get(MeetingNotes, meeting.id)

        if (
            existing is not None
            and not force
            and count - existing.source_line_count < NEW_LINES_THRESHOLD
        ):
            return existing, False

        if count == 0:
            return existing or _empty_notes(db, meeting), False

        try:
            result = _generate(_transcript_text(lines))
        except (anthropic.APIError, RuntimeError, ValueError):
            if existing is not None:
                return existing, True
            notes = _empty_notes(db, meeting)
            return notes, True

        notes = existing or MeetingNotes(meeting_id=meeting.id)
        notes.summary = result["summary"]
        notes.decisions = result["decisions"]
        notes.tasks = result["tasks"]
        notes.questions = result["questions"]
        notes.source_line_count = count
        db.add(notes)
        # ИИ-название совещания — если человек не задал своё.
        if not (meeting.title or "").strip() and result["title"]:
            meeting.title = result["title"]
            db.add(meeting)
        db.commit()
        db.refresh(notes)
        return notes, False


def refresh_notes_quietly(db: Session, meeting: Meeting) -> None:
    """Для вызова из finish: пересчитать по всему транскрипту, молча проглотив
    любые ошибки (в т.ч. отсутствие ключа)."""
    try:
        refresh_notes(db, meeting, force=True)
    except Exception:  # noqa: BLE001 — finish не должен падать из-за заметок
        db.rollback()


# --- Документ для базы знаний ----------------------------------------------


def _yaml_list(items: list[str]) -> str:
    if not items:
        return " []"
    return "\n" + "\n".join(f"  - {item}" for item in items)


def build_document(db: Session, meeting: Meeting, notes: MeetingNotes | None) -> str:
    lines = transcript_lines(db, meeting)
    speakers = sorted({line.speaker for line in lines})
    started = meeting.started_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    title = meeting.title or f"Совещание от {started:%Y-%m-%d %H:%M}"

    parts: list[str] = []
    parts.append("---")
    parts.append(f"title: {title}")
    parts.append("kind: record")
    parts.append(f"date: {started:%Y-%m-%d}")
    parts.append(f"started_at: {started:%Y-%m-%d %H:%M}")
    if meeting.finished_at is not None:
        finished = meeting.finished_at
        if finished.tzinfo is None:
            finished = finished.replace(tzinfo=timezone.utc)
        parts.append(f"finished_at: {finished:%Y-%m-%d %H:%M}")
    parts.append(f"participants:{_yaml_list(speakers)}")
    parts.append("source: durov-os/meeting")
    parts.append(f"meeting_id: {meeting.id}")
    parts.append("---")
    parts.append("")
    parts.append(f"# {title}")
    parts.append("")

    summary = (notes.summary if notes else "").strip()
    parts.append("## Резюме")
    parts.append("")
    parts.append(summary or "_нет_")
    parts.append("")

    for heading, items in (
        ("Решения", notes.decisions if notes else []),
        ("Задачи", notes.tasks if notes else []),
        ("Открытые вопросы", notes.questions if notes else []),
    ):
        parts.append(f"## {heading}")
        parts.append("")
        if items:
            parts.extend(f"- {item}" for item in items)
        else:
            parts.append("_нет_")
        parts.append("")

    parts.append("## Транскрипт")
    parts.append("")
    if lines:
        for line in lines:
            stamp = f"{line.at_ms // 60000:02d}:{(line.at_ms // 1000) % 60:02d}"
            parts.append(f"- **{line.speaker}** [{stamp}]: {line.text}")
    else:
        parts.append("_транскрипт не записан_")
    parts.append("")

    return "\n".join(parts)
