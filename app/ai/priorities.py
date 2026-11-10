"""Section "Задачи": asks Claude to pick 2-3 open tasks the currently
authenticated employee should look at first, out of their own open tasks
(where they're assignee or reviewer) - no invented tasks, Claude only picks
ids out of the real candidate list built below.
"""

import json
from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.ai import cache as ai_cache
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


def generate_task_priorities(db: Session, user: User, force: bool = False):
    from app.ai.task_recommendations import generate
    return generate(db, user, daily=False, load_candidates=_load_candidate_tasks,
                    get_client=_get_client, prompt=SYSTEM_PROMPT, tool_schema=TOOL_SCHEMA, force=force)
