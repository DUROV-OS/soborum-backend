"""Рабочее пространство автономного агента: приоритеты, наблюдения, счётчики повторов.

Всё, что агент помнит между прогонами. Бизнес-данные здесь не меняются.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.autonomy.models import (
    WATCH_ESCALATED,
    WATCH_OPEN,
    WATCH_RESOLVED,
    AgentActionCounter,
    AgentPriority,
    AgentWatch,
)
from app.agents.autonomy.priority import PriorityState, next_priority, scan_order

# Разделы, которые агент сканирует (слаги совпадают с dashboard.SECTION_BUILDERS + users).
SECTIONS = [
    "clients", "production", "installation", "cycle", "warehouse",
    "marketing", "tasks", "accounting", "users",
]
INITIAL_WEIGHT = 5
# Столько раз подряд А-действие сценария без реакции человека — потом только Б.
REPEAT_LIMIT = 3


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def ensure_priorities(db: Session) -> list[AgentPriority]:
    """Первый запуск — создаёт приоритеты по всем разделам."""
    rows = {row.section: row for row in db.scalars(select(AgentPriority))}
    for section in SECTIONS:
        if section not in rows:
            row = AgentPriority(section=section, weight=INITIAL_WEIGHT, signal_streak=0, quiet_streak=0)
            db.add(row)
            rows[section] = row
    db.flush()
    return list(rows.values())


def ordered_sections(db: Session) -> list[str]:
    weights = {row.section: row.weight for row in ensure_priorities(db)}
    return scan_order(weights)


def update_priorities(db: Session, with_signal: set[str], scanned: list[str]) -> None:
    rows = {row.section: row for row in ensure_priorities(db)}
    for section in scanned:
        row = rows[section]
        new = next_priority(
            PriorityState(row.weight, row.signal_streak, row.quiet_streak), section in with_signal
        )
        row.weight, row.signal_streak, row.quiet_streak = new.weight, new.signal_streak, new.quiet_streak
    db.flush()


# -------------------------------------------------------------- наблюдения --

def get_watch(db: Session, key: str) -> AgentWatch | None:
    return db.scalars(select(AgentWatch).where(AgentWatch.key == key)).first()


def touch_watch(
    db: Session, *, key: str, section: str, scenario: str, text: str, escalate_in: timedelta | None
) -> tuple[AgentWatch, bool]:
    """Создаёт или обновляет наблюдение. Второе значение — новое ли оно."""
    now = utcnow()
    watch = get_watch(db, key)
    if watch is None:
        watch = AgentWatch(
            key=key, section=section, scenario=scenario, text=text, status=WATCH_OPEN,
            seen_count=1, created_at=now, last_seen_at=now,
            escalate_after=now + escalate_in if escalate_in else None,
        )
        db.add(watch)
        db.flush()
        return watch, True
    if watch.status == WATCH_RESOLVED:
        # Условие вернулось — наблюдение открывается заново.
        watch.status = WATCH_OPEN
        watch.seen_count = 1
        watch.created_at = now
        watch.last_acted_at = None
        watch.escalate_after = now + escalate_in if escalate_in else None
    else:
        watch.seen_count += 1
    watch.text = text
    watch.last_seen_at = now
    db.flush()
    return watch, False


def due_for_escalation(watch: AgentWatch) -> bool:
    deadline = aware(watch.escalate_after)
    return watch.status == WATCH_OPEN and deadline is not None and deadline <= utcnow()


def mark_escalated(watch: AgentWatch) -> None:
    watch.status = WATCH_ESCALATED
    watch.last_acted_at = utcnow()


def resolve_cleared(db: Session, seen_keys: set[str], scanned_scenarios: set[str]) -> int:
    """Условие исчезло — наблюдение закрывается само. Не трогаем сценарии,
    которые в этом прогоне не проверялись."""
    closed = 0
    for watch in db.scalars(select(AgentWatch).where(AgentWatch.status.in_([WATCH_OPEN, WATCH_ESCALATED]))):
        if watch.scenario in scanned_scenarios and watch.key not in seen_keys:
            watch.status = WATCH_RESOLVED
            closed += 1
    db.flush()
    return closed


def resolve_by_human(db: Session, watch_id: int, user_id: int) -> AgentWatch | None:
    watch = db.get(AgentWatch, watch_id)
    if watch is None:
        return None
    watch.status = WATCH_RESOLVED
    watch.resolved_by_id = user_id
    register_reaction(db, watch.scenario)
    db.flush()
    return watch


# ---------------------------------------------------------------- счётчики --

def get_counter(db: Session, scenario: str) -> AgentActionCounter:
    counter = db.get(AgentActionCounter, scenario)
    if counter is None:
        counter = AgentActionCounter(scenario=scenario, repeats=0)
        db.add(counter)
        db.flush()
    return counter


def must_ask_human(db: Session, scenario: str) -> bool:
    """А-действие этого сценария уже повторялось REPEAT_LIMIT раз без реакции."""
    return get_counter(db, scenario).repeats >= REPEAT_LIMIT


def register_action(db: Session, scenario: str) -> None:
    counter = get_counter(db, scenario)
    counter.repeats += 1
    counter.last_action_at = utcnow()


def register_reaction(db: Session, scenario: str) -> None:
    counter = get_counter(db, scenario)
    counter.repeats = 0
    counter.last_reaction_at = utcnow()
