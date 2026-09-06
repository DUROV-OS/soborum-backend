"""Реестр фоновых задач по расписанию: ежедневных и еженедельных.

Оркестраторы (app/jobs/daily.py, app/jobs/weekly.py) прогоняют
зарегистрированные здесь задачи в порядке регистрации.

Новая задача = функция ``(db, day) -> JobResult`` + декоратор
``@daily_job("имя")`` или ``@weekly_job("имя")``. Ежедневные ходят к модели
на профиле DAILY_JOB (Sonnet, low effort), еженедельные — WEEKLY_JOB
(Sonnet, medium effort); см. app/ai/model_profiles.py.
"""

from __future__ import annotations

import logging
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Callable

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


@dataclass
class JobResult:
    name: str
    status: str  # "ok" | "skipped" | "error"
    detail: str = ""
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None

    @property
    def ok(self) -> bool:
        return self.status != "error"

    def done(self, status: str, detail: str = "") -> "JobResult":
        self.status = status
        self.detail = detail
        self.finished_at = datetime.now(timezone.utc)
        return self


Job = Callable[[Session, date], JobResult]

_DAILY_JOBS: list[tuple[str, Job]] = []
_WEEKLY_JOBS: list[tuple[str, Job]] = []


def daily_job(name: str) -> Callable[[Job], Job]:
    def decorator(fn: Job) -> Job:
        _DAILY_JOBS.append((name, fn))
        return fn

    return decorator


def weekly_job(name: str) -> Callable[[Job], Job]:
    def decorator(fn: Job) -> Job:
        _WEEKLY_JOBS.append((name, fn))
        return fn

    return decorator


def iter_daily_jobs() -> list[tuple[str, Job]]:
    return list(_DAILY_JOBS)


def iter_weekly_jobs() -> list[tuple[str, Job]]:
    return list(_WEEKLY_JOBS)


def run_job(name: str, fn: Job, db: Session, day: date) -> JobResult:
    """Выполнить одну задачу, поглотив исключение в JobResult(status=error),
    чтобы падение одного сборщика не роняло весь прогон."""
    result = JobResult(name=name, status="ok")
    try:
        out = fn(db, day)
        return out if isinstance(out, JobResult) else result.done("ok")
    except Exception as e:  # noqa: BLE001 — намеренно широко: изоляция задач
        db.rollback()
        logger.exception("Задача %s упала", name)
        return result.done("error", f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}")
