"""Публикация результатов агента через существующие точки интеграции (0112).

Уровень А — уведомление ответственным. Уровень Б — карточка в «Задачах» со статусом
«на согласование» (IN_REVIEW, link_type=AGENT_PROPOSAL). Нового UI нет. Каждый текст
явно помечен как инициатива ИИ.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.agents.autonomy.guardrails import assert_allowed
from app.agents.autonomy.playbook import Finding
from app.notifications import service as notifications
from app.notifications.models import NotificationKind
from app.tasks import service as task_service
from app.tasks import timelog
from app.tasks.models import Task, TaskLinkType, TaskPriority, TaskStatus

AI_MARK = "Инициатива ИИ"


def object_type(scenario: str) -> str:
    return f"agent:{scenario}"


def publish_level_a(db: Session, finding: Finding) -> dict:
    """Возвращает запись для журнала. Ничего, кроме уведомлений, не создаёт."""
    assert_allowed("notify", finding.module)
    sent = 0
    for user_id in dict.fromkeys(finding.recipients):
        note = notifications.notify(
            db, user_id, kind=NotificationKind.AGENT_INITIATIVE, module=finding.module,
            title=f"{AI_MARK}: {finding.title}", body=finding.text,
            object_type=object_type(finding.scenario), object_id=None,
        )
        sent += 1 if note else 0
    result = f"уведомлено: {sent}" if sent else "получатели не найдены или отключили уведомления"
    return {"result": result, "notified": sent}


def find_open_proposal(db: Session, key: str) -> Task | None:
    for task in db.query(Task).filter(
        Task.link_type == TaskLinkType.AGENT_PROPOSAL, Task.status == TaskStatus.IN_REVIEW
    ):
        if (task.link_meta or {}).get("key") == key:
            return task
    return None


def publish_level_b(db: Session, finding: Finding, *, reason: str = "") -> dict:
    assert_allowed("create_proposal", finding.module)
    existing = find_open_proposal(db, finding.key)
    if existing is not None:
        return {"result": "карточка уже ждёт решения", "task_id": existing.id}
    body = f"{AI_MARK}. Это не сообщение сотрудника: можно принять, отредактировать или отклонить.\n\n{finding.text}"
    if reason:
        body += f"\n\nПочему на согласование: {reason}."
    task = task_service.create_task(
        db,
        title=f"На согласование · {finding.title}",
        description=body,
        priority=TaskPriority.MEDIUM,
        assignee_ids=list(dict.fromkeys(finding.recipients)),
        link_type=TaskLinkType.AGENT_PROPOSAL,
        link_meta={"scenario": finding.scenario, "key": finding.key, "section": finding.section},
    )
    previous = task.status
    task.status = TaskStatus.IN_REVIEW
    timelog.record_stage(db, task, previous, task.status, automatic=True, note="agent_proposal")
    for user_id in dict.fromkeys(finding.recipients):
        notifications.notify(
            db, user_id, kind=NotificationKind.AGENT_INITIATIVE, module=finding.module,
            title=f"{AI_MARK}: на согласование — {finding.title}", body=finding.text,
            object_type=object_type(finding.scenario), object_id=task.id,
        )
    return {"result": "карточка «на согласование» создана", "task_id": task.id}
