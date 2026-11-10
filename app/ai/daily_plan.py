"""Раздел «Задачи»: ИИ-план на день по сотруднику (0070-e). Отдельная от
app.ai.priorities механика - та выбирает 2-3 задачи «на что посмотреть в
первую очередь», эта собирает связный план на весь рабочий день с оглядкой
на сторипоинты задачи (0070-d), приоритет (0070-a) и текущую загруженность
других сотрудников (агрегированное число, без имён и задач - см.
app.tasks.story_points_stats.average_open_points).

Сторипоинты и агрегаты нагрузки передаются в промпт (внутренний вызов ИИ),
но не возвращаются наружу через DailyPlanOut - скрытость 0070-d для
пользователей сохраняется.
"""

import json
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.ai import cache as ai_cache
from app.ai.schemas import DailyPlanItemOut, DailyPlanOut
from app.core.config import settings
from app.tasks import story_points_stats
from app.tasks.models import Task, TaskStatus
from app.tasks.schemas import TaskOut
from app.users.models import User

SUBMIT_TOOL_NAME = "submit_daily_plan"

MAX_CANDIDATES = 30

# Ориентировочный дневной бюджет очков объёма - разумный набор задач на
# рабочий день, не весь список открытых задач сотрудника.
DAILY_POINT_BUDGET = 6

# Кэш держит план весь день (ключ ниже включает дату, так что смена дня сама
# по себе даёт промах кэша) - более длинный TTL, чем 4-часовой дефолт
# ai_cache.TTL, чтобы план не пересчитывался при каждом открытии страницы.
CACHE_TTL = timedelta(hours=20)

SYSTEM_PROMPT = (
    "Ты — ассистент системы управления производством модульных домов «Soborbum». Тебе передан "
    "список открытых задач одного сотрудника (там, где он исполнитель или проверяющий), с "
    "объёмом («story_points»), приоритетом, дедлайном и просрочкой, плюс средняя текущая "
    "загруженность других сотрудников компании (только число, без имён и задач).\n\n"
    "Правила:\n"
    "- Собери план на сегодня: подмножество задач, которое разумно успеть за один рабочий день, "
    f"ориентируясь на дневной бюджет объёма около {DAILY_POINT_BUDGET} очков (не жёсткий лимит, "
    "но не наваливай весь список).\n"
    "- Просроченные и высокоприоритетные задачи, задачи с близким дедлайном — весомее для "
    "включения в план.\n"
    "- Если сотрудник и так заметно выше средней загруженности других — план может быть короче, "
    "не подкидывай сверху лишнее.\n"
    "- Выбирай ТОЛЬКО из id задач, переданных во входных данных, не придумывай новые.\n"
    "- reason — одно короткое предложение по-русски о том, почему эта задача в сегодняшнем плане.\n"
    "- Порядок позиций в ответе — порядок, в котором стоит браться за задачи.\n"
    "- Отвечай ТОЛЬКО вызовом инструмента submit_daily_plan, без текста."
)

TOOL_SCHEMA = {
    "name": SUBMIT_TOOL_NAME,
    "description": "Отправить план задач на сегодня, в порядке приоритета выполнения.",
    "input_schema": {
        "type": "object",
        "properties": {
            "plan": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "integer"},
                        "reason": {"type": "string"},
                    },
                    "required": ["task_id", "reason"],
                },
            },
        },
        "required": ["plan"],
    },
}


def _get_client():
    if not settings.llm_configured:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="ИИ не настроен: нет ключа активного провайдера (AI_PROVIDER, см. backend/.env)",
        )
    from app.core.llm import llm_client

    return llm_client()


def _load_candidate_tasks(db: Session, user: User) -> list[Task]:
    return (
        db.query(Task)
        .filter(
            Task.status != TaskStatus.DONE,
            or_(Task.assignees.any(User.id == user.id), Task.reviewers.any(User.id == user.id)),
        )
        .all()
    )


def _urgency_key(task: Task, now: datetime):
    is_overdue = task.deadline is not None and task.deadline < now
    deadline = task.deadline or datetime.max.replace(tzinfo=timezone.utc)
    return (not is_overdue, deadline)


def _serialize_candidate(task: Task, user: User, now: datetime) -> dict:
    roles = []
    if any(u.id == user.id for u in task.assignees):
        roles.append("assignee")
    if any(u.id == user.id for u in task.reviewers):
        roles.append("reviewer")
    return {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "story_points": task.story_points,
        "priority": task.priority.value,
        "deadline": task.deadline.isoformat() if task.deadline else None,
        "is_overdue": bool(task.deadline and task.deadline < now),
        "your_role": roles,
    }


def generate_daily_plan(db: Session, user: User, force: bool = False):
    from app.ai.task_recommendations import generate
    return generate(db, user, daily=True, load_candidates=_load_candidate_tasks,
                    get_client=_get_client, prompt=SYSTEM_PROMPT, tool_schema=TOOL_SCHEMA, force=force)
