"""Отправка сообщений в MAX от имени агента — только с согласия человека (0112).

Агент может лишь *предложить* сообщение: карточка «на согласование» с чатом и текстом.
Отправляет не агент, а `approve_and_send`, которую вызывает эндпоинт администратора.
Из прогона (`runner`) этот модуль не импортируется и сценарии `external_send`
по-прежнему запрещены в `guardrails.assert_allowed`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.agents.autonomy.publish import AI_MARK
from app.tasks import service as task_service
from app.tasks import timelog
from app.tasks.models import Task, TaskLinkType, TaskPriority, TaskStatus
from app.users.models import User

SCENARIO = "max_send"
MAX_TEXT_LEN = 4000


def _is_max_card(task: Task) -> bool:
    return task.link_type == TaskLinkType.AGENT_PROPOSAL and (task.link_meta or {}).get("scenario") == SCENARIO


def propose_message(
    db: Session, *, chat_id: int, text: str, recipient: str, recipient_ids: list[int], reason: str = ""
) -> Task:
    """Создаёт карточку «На согласование · Сообщение в MAX». Ничего не отправляет."""
    text = (text or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="Пустой текст сообщения")
    if len(text) > MAX_TEXT_LEN:
        raise HTTPException(status_code=422, detail=f"Текст длиннее {MAX_TEXT_LEN} символов")
    body = (
        f"{AI_MARK}. Сообщение в MAX не отправлено — ждёт вашего решения.\n\n"
        f"Кому: {recipient}\n\nТекст:\n{text}"
    )
    if reason:
        body += f"\n\nПочему: {reason}."
    task = task_service.create_task(
        db,
        title=f"На согласование · Сообщение в MAX: {recipient}",
        description=body,
        priority=TaskPriority.MEDIUM,
        assignee_ids=list(dict.fromkeys(recipient_ids)),
        link_type=TaskLinkType.AGENT_PROPOSAL,
        link_meta={
            "scenario": SCENARIO,
            "key": f"{SCENARIO}:{chat_id}:{hashlib.sha1(text.encode()).hexdigest()[:10]}",
            "section": "agents",
            "max": {"chat_id": chat_id, "text": text, "recipient": recipient},
        },
    )
    previous = task.status
    task.status = TaskStatus.IN_REVIEW
    timelog.record_stage(db, task, previous, task.status, automatic=True, note="agent_proposal")
    return task


def approve_and_send(db: Session, task_id: int, admin: User, sender: Callable[..., dict]) -> dict:
    """Единственное место, где сообщение уходит в MAX. Только по карточке в статусе
    «на согласование» и один раз: после отправки карточка закрывается."""
    task = db.get(Task, task_id)
    if task is None or not _is_max_card(task):
        raise HTTPException(status_code=404, detail="Карточка сообщения в MAX не найдена")
    if task.status != TaskStatus.IN_REVIEW:
        raise HTTPException(status_code=409, detail="Карточка уже обработана")
    payload = (task.link_meta or {}).get("max") or {}
    chat_id, text = payload.get("chat_id"), payload.get("text")
    if not chat_id or not text:
        raise HTTPException(status_code=422, detail="В карточке нет чата или текста")
    result = sender(chat_id, text)  # при ошибке MAX поднимет HTTPException — карточка остаётся открытой
    task_service.close_with_resolution(
        db, task, admin, comment=f"Отправлено в MAX после согласования администратором ({payload.get('recipient', chat_id)})."
    )
    db.commit()
    return {"task_id": task.id, "chat_id": result.get("chatId", chat_id)}


def reject(db: Session, task_id: int, admin: User) -> None:
    task = db.get(Task, task_id)
    if task is None or not _is_max_card(task):
        raise HTTPException(status_code=404, detail="Карточка сообщения в MAX не найдена")
    if task.status != TaskStatus.IN_REVIEW:
        raise HTTPException(status_code=409, detail="Карточка уже обработана")
    task_service.close_with_resolution(db, task, admin, comment="Отклонено администратором: сообщение в MAX не отправлено.")
    db.commit()
