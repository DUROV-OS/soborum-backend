"""Схемы Pydantic для автономного агента."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class AutoRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    started_at: datetime
    finished_at: datetime | None
    trigger: str
    sections: list[str]
    deviations: list[dict]
    actions: list[dict]
    summary: str
    error: str | None


class PriorityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    section: str
    weight: int
    signal_streak: int
    quiet_streak: int


class WatchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    key: str
    section: str
    scenario: str
    text: str
    status: str
    seen_count: int
    escalate_after: datetime | None
    created_at: datetime
    last_seen_at: datetime


class CounterOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    scenario: str
    repeats: int
    last_action_at: datetime | None
    last_reaction_at: datetime | None


class AutonomyStateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    runs: list[AutoRunOut]
    priorities: list[PriorityOut]
    watchlist: list[WatchOut]
    counters: list[CounterOut]
    pending_proposals: int
    next_run_hint: str
