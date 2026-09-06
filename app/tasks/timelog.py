"""Stage-transition time tracking for tasks.

This module is pure bookkeeping: it never changes task behaviour or the
Tasks API surface. Every public function is called for its side effect - append
a `TaskStageEvent` row and refresh the derived `TaskWorkDuration` rollup - from
app.tasks.service at the exact point a task's status changes.

The data is meant to feed two things downstream (no UI yet):
  * employee KPI - `TaskStageEvent.actor_id` says who moved a task through each
    stage and when, so time-per-stage can be attributed per person;
  * AI estimation - `TaskWorkDuration` gives, per finished task, how long it sat
    in each stage and the total time it was actually worked on, which is the
    training signal for "a task like this usually takes N hours".
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.tasks.models import Task, TaskStageEvent, TaskStatus, TaskWorkDuration
from app.users.models import User

# Stages that count as the task being genuinely worked on (as opposed to
# waiting in the backlog / blocked).
_ACTIVE_STATUSES = (TaskStatus.IN_PROGRESS, TaskStatus.IN_REVIEW)


def _aware(dt: datetime | None) -> datetime | None:
    """Normalise to tz-aware UTC. Postgres round-trips aware datetimes, SQLite
    (tests) strips the tzinfo - this keeps arithmetic between the two safe."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def record_stage(
    db: Session,
    task: Task,
    from_status: TaskStatus | None,
    to_status: TaskStatus,
    *,
    actor: User | None = None,
    automatic: bool = False,
    note: str | None = None,
) -> TaskStageEvent:
    """Append one transition to the log and recompute the task's rollup.

    `from_status=None` marks the task-creation row. `actor` is the user who
    triggered a manual transition; leave it None for automatic ones and pass
    `automatic=True` so they are distinguishable in reporting.
    """
    event = TaskStageEvent(
        task_id=task.id,
        from_status=from_status,
        to_status=to_status,
        actor_id=actor.id if actor is not None else None,
        automatic=automatic,
        note=note,
    )
    db.add(event)
    db.flush()
    _recompute(db, task)
    return event


def _recompute(db: Session, task: Task) -> None:
    """Rebuild TaskWorkDuration for `task` from its full event log.

    Walks consecutive events: between event i and i+1 the task was in
    `events[i].to_status` for that wall-clock gap. Only closed intervals are
    counted, so the totals are final once the task reaches DONE and monotonic
    before that.
    """
    events = (
        db.query(TaskStageEvent)
        .filter(TaskStageEvent.task_id == task.id)
        .order_by(TaskStageEvent.created_at, TaskStageEvent.id)
        .all()
    )
    if not events:
        return

    row = db.get(TaskWorkDuration, task.id)
    if row is None:
        row = TaskWorkDuration(task_id=task.id)
        db.add(row)

    by_status: dict[str, float] = {}
    first_started_at: datetime | None = None
    for earlier, later in zip(events, events[1:]):
        seconds = (_aware(later.created_at) - _aware(earlier.created_at)).total_seconds()
        if seconds > 0:
            key = earlier.to_status.value
            by_status[key] = by_status.get(key, 0.0) + seconds
        if earlier.to_status == TaskStatus.IN_PROGRESS and first_started_at is None:
            first_started_at = earlier.created_at

    if first_started_at is None:
        first_started_at = next(
            (e.created_at for e in events if e.to_status == TaskStatus.IN_PROGRESS), None
        )

    last = events[-1]
    completed_at = last.created_at if last.to_status == TaskStatus.DONE else None

    row.seconds_by_status = {key: int(round(value)) for key, value in by_status.items()}
    row.total_working_seconds = int(
        round(sum(by_status.get(s.value, 0.0) for s in _ACTIVE_STATUSES))
    )
    row.first_started_at = first_started_at
    row.completed_at = completed_at
    row.is_complete = completed_at is not None
    if completed_at is not None:
        row.total_lead_seconds = int(
            round((_aware(completed_at) - _aware(events[0].created_at)).total_seconds())
        )
    else:
        row.total_lead_seconds = 0
    row.updated_at = datetime.now(timezone.utc)
    db.flush()
