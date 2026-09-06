"""Section "Задачи": asks Claude to pick 2-3 open tasks the currently
authenticated employee should look at first, out of their own open tasks
(where they're assignee or reviewer) - no invented tasks, Claude only picks
ids out of the real candidate list built below.
"""

import json
from datetime import datetime, timezone

import anthropic
from fastapi import HTTPException, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.ai import cache as ai_cache
from app.ai import model_profiles
from app.ai.schemas import PriorityTaskOut, TaskPrioritiesOut
from app.core.config import settings
from app.tasks.models import Task, TaskStatus
from app.tasks.schemas import TaskOut
from app.users.models import User

SUBMIT_TOOL_NAME = "submit_task_priorities"

MAX_CANDIDATES = 30

SYSTEM_PROMPT = (
    "Ты — ассистент системы управления производством модульных домов «Soborbum». Тебе передан "
    "список открытых задач одного сотрудника (там, где он исполнитель или проверяющий), с "
    "метаданными о просрочке, дедлайне и незавершённых задачах, которые от неё зависят или которые "
    "она блокирует.\n\n"
    "Правила:\n"
    "- Выбери 2-3 задачи, к которым сотруднику приоритетно присмотреться прямо сейчас: в первую "
    "очередь просроченные, с близким дедлайном или блокирующие другие задачи. Если подходящих задач "
    "меньше двух — верни столько, сколько есть.\n"
    "- Выбирай ТОЛЬКО из id задач, переданных во входных данных, не придумывай новые.\n"
    "- reason — одно короткое предложение по-русски о том, почему именно эта задача важна сейчас.\n"
    "- Отвечай ТОЛЬКО вызовом инструмента submit_task_priorities, без текста."
)

TOOL_SCHEMA = {
    "name": SUBMIT_TOOL_NAME,
    "description": "Отправить 2-3 задачи, к которым сотруднику стоит присмотреться в первую очередь.",
    "input_schema": {
        "type": "object",
        "properties": {
            "priorities": {
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
        "required": ["priorities"],
    },
}


def _get_client() -> anthropic.Anthropic:
    if not settings.anthropic_api_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="ИИ не настроен: не задан ANTHROPIC_API_KEY (см. backend/.env)",
        )
    return anthropic.Anthropic(api_key=settings.anthropic_api_key)


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
    blocks_open = sum(1 for t in task.blocks if t.status != TaskStatus.DONE)
    return (not is_overdue, deadline, -blocks_open)


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
        "status": task.status.value,
        "deadline": task.deadline.isoformat() if task.deadline else None,
        "is_overdue": bool(task.deadline and task.deadline < now),
        "your_role": roles,
        "blocks_open_tasks": sum(1 for t in task.blocks if t.status != TaskStatus.DONE),
        "depends_on_open_tasks": [t.id for t in task.depends_on if t.status != TaskStatus.DONE],
    }


def generate_task_priorities(db: Session, user: User, force: bool = False) -> TaskPrioritiesOut:
    now = datetime.now(timezone.utc)
    all_candidates_by_id = {t.id: t for t in _load_candidate_tasks(db, user)}

    # Cached picks are just (task_id, reason) pairs - the AI's opinion, which
    # is what's slow and worth reusing. The task itself is always re-read
    # live here, so a task that's since been completed or changed doesn't
    # show stale for up to 4 hours.
    cache_key = f"task_priorities:{user.id}"
    cached = ai_cache.get(db, cache_key, force)
    if cached is not None:
        priorities = []
        for item in cached.get("picks", []):
            task = all_candidates_by_id.get(item.get("task_id"))
            if task is not None:
                priorities.append(PriorityTaskOut(task=TaskOut.from_model(task), reason=item.get("reason", "")))
        return TaskPrioritiesOut(generated_at=cached["generated_at"], priorities=priorities)

    candidates = sorted(all_candidates_by_id.values(), key=lambda t: _urgency_key(t, now))
    if not candidates:
        return TaskPrioritiesOut(generated_at=now, priorities=[])

    candidates = candidates[:MAX_CANDIDATES]
    by_id = {t.id: t for t in candidates}

    client = _get_client()
    response = client.messages.create(
        **model_profiles.profile_params(model_profiles.ModelProfile.QUICK),
        max_tokens=768,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": f"Открытые задачи сотрудника «{user.full_name}» на "
                f"{now.date().isoformat()}, где он исполнитель или проверяющий:\n\n"
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
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="ИИ не вернул список задач")

    priorities: list[PriorityTaskOut] = []
    seen: set[int] = set()
    for item in tool_use.input.get("priorities", []):
        task = by_id.get(item.get("task_id"))
        if task is None or task.id in seen:
            continue
        seen.add(task.id)
        priorities.append(PriorityTaskOut(task=TaskOut.from_model(task), reason=item.get("reason", "")))
        if len(priorities) == 3:
            break

    picks = [{"task_id": p.task.id, "reason": p.reason} for p in priorities]
    ai_cache.set(db, cache_key, {"picks": picks, "generated_at": now.isoformat()}, now)
    return TaskPrioritiesOut(generated_at=now, priorities=priorities)
