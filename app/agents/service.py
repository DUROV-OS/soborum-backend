from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.ids import RU_LABELS, AgentId
from app.agents.models import AgentRun
from app.agents.runtime import run_task
from app.agents.schemas import (
    AgentRunOut,
    AgentsStatsOut,
    DayPointOut,
    LegalMixOut,
    RouteShareOut,
    TotalsOut,
    TraceOut,
)
from app.users.models import User

MSK = ZoneInfo("Europe/Moscow")
TRACE_LIMIT = 40
WEEK_DAYS = 7


def create_run(db: Session, user: User, text: str) -> AgentRunOut:
    try:
        result = run_task(text)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error

    specialists = [agent.value for agent in result.route.specialists]
    row = AgentRun(
        trace_id=result.trace_id,
        text=text.strip(),
        reply=result.reply,
        legal_verdict=result.legal.verdict.value,
        legal_rules=[finding.rule_id for finding in result.legal.findings],
        legal_passport=result.legal.passport,
        released=result.released,
        specialists=specialists,
        scores={agent.value: score for agent, score in result.route.scores.items()},
        created_by_id=user.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _run_out(row)


def list_runs(db: Session, limit: int = TRACE_LIMIT) -> list[AgentRunOut]:
    rows = db.scalars(select(AgentRun).order_by(AgentRun.created_at.desc()).limit(limit)).all()
    return [_run_out(row) for row in rows]


def stats(db: Session) -> AgentsStatsOut:
    rows = db.scalars(select(AgentRun).order_by(AgentRun.created_at.desc())).all()
    mix = LegalMixOut()
    for row in rows:
        if row.legal_verdict == "allow":
            mix.allow += 1
        elif row.legal_verdict == "allow_with_conditions":
            mix.allow_with_conditions += 1
        elif row.legal_verdict == "escalate_human":
            mix.escalate_human += 1
        elif row.legal_verdict == "block":
            mix.block += 1

    routed: Counter[str] = Counter()
    for row in rows:
        routed["coordinator"] += 1
        for agent_id in row.specialists:
            routed[agent_id] += 1

    routing = [
        RouteShareOut(id=agent_id.value, title=RU_LABELS[agent_id], count=routed[agent_id.value])
        for agent_id in AgentId
        if routed[agent_id.value]
    ]
    routing.sort(key=lambda item: (-item.count, item.id))

    totals = TotalsOut(
        runs=len(rows),
        blocked=mix.block,
        escalated=mix.escalate_human,
        released=sum(1 for row in rows if row.released),
    )
    return AgentsStatsOut(
        week=_week(rows),
        legal=mix,
        routing=routing,
        traces=[_trace_out(row) for row in rows[:TRACE_LIMIT]],
        totals=totals,
    )


def _run_out(row: AgentRun) -> AgentRunOut:
    titles = [_title(agent_id) for agent_id in row.specialists]
    return AgentRunOut(
        id=row.id,
        trace_id=row.trace_id,
        text=row.text,
        reply=row.reply,
        legal_verdict=row.legal_verdict,  # type: ignore[arg-type]
        legal_rules=list(row.legal_rules or []),
        legal_passport=row.legal_passport,
        released=row.released,
        specialists=list(row.specialists or []),
        specialist_titles=titles,
        created_at=row.created_at,
    )


def _trace_out(row: AgentRun) -> TraceOut:
    return TraceOut(
        id=row.id,
        trace_id=row.trace_id,
        text=row.text,
        agents=[_title(agent_id) for agent_id in row.specialists],
        legal=row.legal_verdict,  # type: ignore[arg-type]
        released=row.released,
        created_at=row.created_at,
    )


def _title(agent_id: str) -> str:
    try:
        return RU_LABELS[AgentId(agent_id)]
    except ValueError:
        return agent_id


def _week(rows: list[AgentRun]) -> list[DayPointOut]:
    today = datetime.now(MSK).date()
    buckets = {today - timedelta(days=offset): DayPointOut(date="", runs=0, blocked=0, escalated=0, released=0) for offset in range(WEEK_DAYS - 1, -1, -1)}
    for row in rows:
        when = row.created_at
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        day = when.astimezone(MSK).date()
        point = buckets.get(day)
        if point is None:
            continue
        point.runs += 1
        if row.legal_verdict == "block":
            point.blocked += 1
        elif row.released:
            point.released += 1
        else:
            point.escalated += 1
    return [
        DayPointOut(
            date=day.strftime("%d.%m"),
            runs=point.runs,
            blocked=point.blocked,
            escalated=point.escalated,
            released=point.released,
        )
        for day, point in buckets.items()
    ]
