"""Чтение состояния рабочего пространства агента и ручной запуск для «Панели агентов»."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.autonomy import runner
from app.agents.autonomy import workspace as ws
from app.agents.autonomy.models import AgentActionCounter, AgentAutoRun, AgentWatch
from app.agents.autonomy.schemas import AutonomyStateOut, AutoRunOut, WatchOut
from app.tasks.models import Task, TaskLinkType, TaskStatus
from app.users.models import User

RUNS_LIMIT = 20


def state(db: Session) -> AutonomyStateOut:
    priorities = sorted(ws.ensure_priorities(db), key=lambda p: (-p.weight, p.section))
    db.commit()
    runs = db.scalars(select(AgentAutoRun).order_by(AgentAutoRun.started_at.desc()).limit(RUNS_LIMIT)).all()
    watchlist = db.scalars(
        select(AgentWatch).where(AgentWatch.status != "resolved").order_by(AgentWatch.last_seen_at.desc())
    ).all()
    counters = db.scalars(select(AgentActionCounter).order_by(AgentActionCounter.scenario)).all()
    pending = db.query(Task).filter(
        Task.link_type == TaskLinkType.AGENT_PROPOSAL, Task.status == TaskStatus.IN_REVIEW
    ).count()
    return AutonomyStateOut(
        runs=[AutoRunOut.model_validate(r) for r in runs],
        priorities=priorities,
        watchlist=[WatchOut.model_validate(w) for w in watchlist],
        counters=counters,
        pending_proposals=pending,
        next_run_hint=runner.next_run_hint(db),
    )


def run_manual(db: Session) -> AutoRunOut:
    return AutoRunOut.model_validate(runner.run_once(db, trigger="manual"))


def resolve_watch(db: Session, watch_id: int, admin: User) -> WatchOut:
    watch = ws.resolve_by_human(db, watch_id, admin.id)
    if watch is None:
        raise HTTPException(status_code=404, detail="Наблюдение не найдено")
    db.commit()
    return WatchOut.model_validate(watch)
