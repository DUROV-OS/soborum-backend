from fastapi import Depends, FastAPI, Query
from sqlalchemy.orm import Session

from app.agents import service as agents_service
from app.agents.autonomy import service as autonomy_service
from app.agents.autonomy.schemas import AutonomyStateOut, AutoRunOut, WatchOut
from app.agents.schemas import (
    AgentRunOut,
    AgentsStatsOut,
    ApprovalDecisionRequest,
    ApprovalOut,
    CreateRunRequest,
    ShiftOut,
)
from app.core.deps import get_current_user, require_admin
from app.db.session import get_db
from app.users.models import User

app = FastAPI(
    title="Soborbum — Агенты",
    description=(
        "Операционная команда из восьми ролей: координатор, legal gate, маршрут. "
        "Не Марина и не Совет директоров."
    ),
    version="0.1",
)


@app.post("/runs", response_model=AgentRunOut)
def create_run(
    body: CreateRunRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return agents_service.create_run(db, user, body.text)


@app.get("/runs", response_model=list[AgentRunOut])
def list_runs(
    limit: int = Query(40, ge=1, le=200),
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    return agents_service.list_runs(db, limit)


@app.get("/stats", response_model=AgentsStatsOut)
def get_stats(
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    return agents_service.stats(db)


@app.post("/shifts", response_model=ShiftOut)
def create_shift(
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    return agents_service.create_shift(db, user)


@app.get("/shifts/latest", response_model=ShiftOut | None)
def get_latest_shift(
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    return agents_service.latest_shift(db)


@app.post("/approvals/{approval_id}/decision", response_model=ApprovalOut)
def decide_approval(
    approval_id: int,
    body: ApprovalDecisionRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    return agents_service.decide_approval(db, user, approval_id, body.status, body.subject_hash)


@app.get("/autonomy/state", response_model=AutonomyStateOut)
def get_autonomy_state(
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    return autonomy_service.state(db)


@app.post("/autonomy/run", response_model=AutoRunOut)
def run_autonomy(
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    return autonomy_service.run_manual(db)


@app.post("/autonomy/watchlist/{watch_id}/resolve", response_model=WatchOut)
def resolve_watch(
    watch_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    return autonomy_service.resolve_watch(db, watch_id, admin)


@app.post("/autonomy/proposals/close")
def close_autonomy_proposals(
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    return {"closed": autonomy_service.close_proposals(db, admin)}
