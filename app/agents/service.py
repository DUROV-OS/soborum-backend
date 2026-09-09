from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from fastapi import status as http_status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.agents.connectors import live_charts
from app.agents.ids import RU_LABELS, AgentId
from app.agents.models import AgentApproval, AgentRun, AgentShift, AgentShiftItem
from app.agents.runtime import run_task
from app.agents.schemas import (
    AgentRunOut,
    AgentsStatsOut,
    ApprovalOut,
    DayPointOut,
    LegalMixOut,
    RouteShareOut,
    ShiftChartOut,
    ShiftItemOut,
    ShiftOut,
    ShiftReviewOut,
    TotalsOut,
    TraceOut,
)
from app.agents.shift import run_shift
from app.core.config import settings
from app.users.models import User

MSK = ZoneInfo("Europe/Moscow")
TRACE_LIMIT = 40
WEEK_DAYS = 7


def create_run(db: Session, user: User, text: str) -> AgentRunOut:
    try:
        result = run_task(text, db=db)
    except ValueError as error:
        raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail=str(error)) from error

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
    shift_count = db.query(AgentShift).count()
    pending = db.query(AgentApproval).filter(AgentApproval.status == "pending").count()
    return AgentsStatsOut(
        week=_week(rows),
        legal=mix,
        routing=routing,
        traces=[_trace_out(row) for row in rows[:TRACE_LIMIT]],
        totals=totals,
        shifts=shift_count,
        pending_approvals=pending,
    )


def maybe_tick_shift(db: Session) -> ShiftOut | None:
    last = db.scalars(select(AgentShift).order_by(AgentShift.created_at.desc()).limit(1)).first()
    if last is not None:
        when = last.created_at
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - when.astimezone(timezone.utc)
        if age.total_seconds() < settings.agent_shift_interval_seconds:
            return None
    return create_shift(db, user=None)


def create_shift(db: Session, user: User | None = None) -> ShiftOut:
    draft = run_shift(db)
    shift = AgentShift(
        verdict=draft.verdict,
        summary=draft.summary,
        claude_used=draft.claude_used,
        created_by_id=user.id if user is not None else None,
        created_at=datetime.now(timezone.utc),
    )
    db.add(shift)
    db.flush()

    item_rows: dict[str, AgentShiftItem] = {}
    for item in draft.items:
        row = AgentShiftItem(
            shift_id=shift.id,
            agent_id=item.agent.value,
            daily_question=item.daily_question,
            stance=item.stance,
            citations=item.citations,
            legal_verdict=item.legal_verdict,
            has_live_data=item.has_live_data,
            reviews=[
                {
                    "reviewer": review.reviewer.value,
                    "text": review.text,
                    "escalate": review.escalate,
                    "kind": review.kind,
                }
                for review in item.reviews
            ],
        )
        db.add(row)
        db.flush()
        item_rows[item.agent.value] = row
        _store_run(db, user, item.daily_question, item.stance, item.legal_verdict, [item.agent.value])

    for approval in draft.approvals:
        item_row = item_rows.get(approval.agent.value)
        db.add(
            AgentApproval(
                shift_id=shift.id,
                item_id=item_row.id if item_row is not None else None,
                kind=approval.kind,
                title=approval.title,
                detail=approval.detail,
                status="pending",
                created_at=datetime.now(timezone.utc),
            )
        )
    db.commit()
    return _shift_out(_load_shift(db, shift.id), db)


def latest_shift(db: Session) -> ShiftOut | None:
    row = db.scalars(
        select(AgentShift)
        .options(selectinload(AgentShift.items), selectinload(AgentShift.approvals))
        .order_by(AgentShift.created_at.desc())
        .limit(1)
    ).first()
    return _shift_out(row, db) if row else None


def _load_shift(db: Session, shift_id: int) -> AgentShift:
    row = db.scalars(
        select(AgentShift)
        .options(selectinload(AgentShift.items), selectinload(AgentShift.approvals))
        .where(AgentShift.id == shift_id)
    ).first()
    if row is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Смены нет")
    return row


def decide_approval(db: Session, user: User, approval_id: int, decision: str) -> ApprovalOut:
    approval = db.get(AgentApproval, approval_id)
    if approval is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Карточки очереди нет")
    if approval.status != "pending":
        raise HTTPException(status_code=http_status.HTTP_409_CONFLICT, detail="Решение уже принято")
    approval.status = decision
    approval.decided_at = datetime.now(timezone.utc)
    approval.decided_by_id = user.id
    db.commit()
    db.refresh(approval)
    return _approval_out(approval)


def _store_run(
    db: Session, user: User | None, text: str, reply: str, legal_verdict: str, specialists: list[str]
) -> None:
    from app.agents.runtime import _trace_id

    db.add(
        AgentRun(
            trace_id=_trace_id(),
            text=text,
            reply=reply,
            legal_verdict=legal_verdict,
            legal_rules=[],
            legal_passport="",
            released=legal_verdict == "allow",
            specialists=specialists,
            scores={},
            created_by_id=user.id if user is not None else None,
            created_at=datetime.now(timezone.utc),
        )
    )


def _shift_out(row: AgentShift, db: Session) -> ShiftOut:
    when = row.created_at
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return ShiftOut(
        id=row.id,
        verdict=row.verdict,  # type: ignore[arg-type]
        summary=row.summary,
        claude_used=row.claude_used,
        created_at=row.created_at,
        items=[_item_out(item) for item in row.items],
        approvals=[_approval_out(approval) for approval in row.approvals],
        charts=[ShiftChartOut.model_validate(chart) for chart in live_charts(db, wait=False)],
        autorun=settings.agent_shift_autorun,
        interval_seconds=settings.agent_shift_interval_seconds,
        next_tick_at=when + timedelta(seconds=settings.agent_shift_interval_seconds),
    )


def _item_out(row: AgentShiftItem) -> ShiftItemOut:
    reviews = []
    for review in row.reviews or []:
        reviewer = str(review.get("reviewer", ""))
        reviews.append(
            ShiftReviewOut(
                reviewer=reviewer,
                reviewer_title=_title(reviewer),
                text=str(review.get("text", "")),
                escalate=bool(review.get("escalate")),
                kind=str(review.get("kind", "ops")),
            )
        )
    return ShiftItemOut(
        id=row.id,
        agent_id=row.agent_id,
        agent_title=_title(row.agent_id),
        daily_question=row.daily_question,
        stance=row.stance,
        citations=list(row.citations or []),
        legal_verdict=row.legal_verdict,  # type: ignore[arg-type]
        has_live_data=bool(row.has_live_data),
        reviews=reviews,
    )


def _approval_out(row: AgentApproval) -> ApprovalOut:
    return ApprovalOut(
        id=row.id,
        shift_id=row.shift_id,
        kind=row.kind,
        title=row.title,
        detail=row.detail,
        status=row.status,
        created_at=row.created_at,
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
