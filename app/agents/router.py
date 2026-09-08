from fastapi import Depends, FastAPI, Query
from sqlalchemy.orm import Session

from app.agents import service as agents_service
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
    return agents_service.decide_approval(db, user, approval_id, body.status)
