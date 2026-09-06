from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

LegalVerdictName = Literal["allow", "allow_with_conditions", "block", "escalate_human"]


class CreateRunRequest(BaseModel):
    text: str = Field(..., min_length=3, max_length=4000)


class AgentRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    trace_id: str
    text: str
    reply: str
    legal_verdict: LegalVerdictName
    legal_rules: list[str]
    legal_passport: str
    released: bool
    specialists: list[str]
    specialist_titles: list[str]
    created_at: datetime


class TraceOut(BaseModel):
    id: int
    trace_id: str
    text: str
    agents: list[str]
    legal: LegalVerdictName
    released: bool
    created_at: datetime


class DayPointOut(BaseModel):
    date: str
    runs: int
    blocked: int
    escalated: int
    released: int


class RouteShareOut(BaseModel):
    id: str
    title: str
    count: int


class LegalMixOut(BaseModel):
    allow: int = 0
    allow_with_conditions: int = 0
    escalate_human: int = 0
    block: int = 0


class TotalsOut(BaseModel):
    runs: int
    blocked: int
    escalated: int
    released: int


class AgentsStatsOut(BaseModel):
    week: list[DayPointOut]
    legal: LegalMixOut
    routing: list[RouteShareOut]
    traces: list[TraceOut]
    totals: TotalsOut
