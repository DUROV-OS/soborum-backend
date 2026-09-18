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

import anthropic
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


def _get_client() -> anthropic.Anthropic:
    if not settings.anthropic_api_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="ИИ не настроен: не задан ANTHROPIC_API_KEY (см. backend/.env)",
        )
    from app.core.llm import anthropic_client

    return anthropic_client()


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


def generate_daily_plan(db: Session, user: User, force: bool = False) -> DailyPlanOut:
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    all_candidates_by_id = {t.id: t for t in _load_candidate_tasks(db, user)}

    cache_key = f"daily_plan:{user.id}:{today}"
    cached = ai_cache.get(db, cache_key, force, ttl=CACHE_TTL)
    if cached is not None:
        plan = []
        for item in cached.get("plan", []):
            task = all_candidates_by_id.get(item.get("task_id"))
            if task is not None:
                plan.append(DailyPlanItemOut(task=TaskOut.from_model(task), reason=item.get("reason", "")))
        return DailyPlanOut(generated_at=cached["generated_at"], plan=plan)

    candidates = sorted(all_candidates_by_id.values(), key=lambda t: _urgency_key(t, now))
    if not candidates:
        return DailyPlanOut(generated_at=now, plan=[])

    candidates = candidates[:MAX_CANDIDATES]
    by_id = {t.id: t for t in candidates}

    others_average_load = story_points_stats.average_open_points(db, exclude_user_id=user.id)

    client = _get_client()
    response = client.messages.create(
        model=settings.ai_model,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": f"Открытые задачи сотрудника «{user.full_name}» на "
                f"{today}, где он исполнитель или проверяющий. Средняя текущая "
                f"загруженность остальных сотрудников: {others_average_load:.1f} очков.\n\n"
                + json.dumps(
                    [_serialize_candidate(t, user, now) for t in candidates],
                    ensure_ascii=False,
                    default=str,
                ),
            }
        ],
        tools=[TOOL_SCHEMA],
        tool_choice={"type": "tool", "name": SUBMIT_TOOL_NAME},
    )

    tool_use = next((block for block in response.content if block.type == "tool_use"), None)
    if tool_use is None:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="ИИ не вернул план на день")

    plan: list[DailyPlanItemOut] = []
    seen: set[int] = set()
    for item in tool_use.input.get("plan", []):
        task = by_id.get(item.get("task_id"))
        if task is None or task.id in seen:
            continue
        seen.add(task.id)
        plan.append(DailyPlanItemOut(task=TaskOut.from_model(task), reason=item.get("reason", "")))

    picks = [{"task_id": p.task.id, "reason": p.reason} for p in plan]
    ai_cache.set(db, cache_key, {"plan": picks, "generated_at": now.isoformat()}, now)
    return DailyPlanOut(generated_at=now, plan=plan)
