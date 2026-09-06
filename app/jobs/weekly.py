"""Еженедельный оркестратор.

Симметричен app/jobs/daily.py, но прогоняет задачи, зарегистрированные через
``@weekly_job(...)``. Такие задачи обращаются к модели на профиле
WEEKLY_JOB (Sonnet, medium effort) — против DAILY_JOB (Sonnet, low) у
ежедневных; см. app/ai/model_profiles.py.

Запуск (обычно из cron раз в неделю):

    python -m app.jobs.weekly                # опорная дата — вчера
    python -m app.jobs.weekly --date 2026-09-01

Опорная дата передаётся задаче как есть; недельное окно (последние 7 дней и
т.п.) каждая задача считает от неё сама. Пока еженедельных задач нет —
оркестратор просто отработает вхолостую; каркас готов под будущие.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

from app.db.session import SessionLocal
from app.jobs.dates import iso, yesterday
from app.jobs.registry import JobResult, iter_weekly_jobs, run_job

# Импорт ради регистрации задач в реестре (появятся — добавить сюда).
from app.telegram import ingest as _tg_ingest  # noqa: F401

logger = logging.getLogger("app.jobs.weekly")


def run_weekly(day: date | None = None, *, only: str | None = None) -> list[JobResult]:
    day = day or yesterday()
    results: list[JobResult] = []

    with SessionLocal() as db:
        jobs = iter_weekly_jobs()
        if not jobs:
            logger.info("Еженедельных задач не зарегистрировано — нечего делать")
        for name, fn in jobs:
            if only and name != only:
                continue
            logger.info("→ еженедельная задача %s (опорная дата %s)", name, iso(day))
            results.append(run_job(name, fn, db, day))

    return results


def _print_report(day: date, results: list[JobResult]) -> None:
    print(f"\n=== Еженедельный прогон (опорная дата {iso(day)}) ===")
    if not results:
        print("[SKIP   ] нет зарегистрированных еженедельных задач")
    for r in results:
        secs = (r.finished_at - r.started_at).total_seconds() if r.finished_at else 0.0
        print(f"[{r.status.upper():7}] {r.name} ({secs:.1f}s) — {r.detail.strip()[:500]}")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Еженедельный оркестратор Durov OS")
    parser.add_argument("--date", help="ГГГГ-ММ-ДД, опорная дата; по умолчанию вчера")
    parser.add_argument("--only", help="запустить только задачу с этим именем")
    args = parser.parse_args(argv)

    day = date.fromisoformat(args.date) if args.date else yesterday()
    results = run_weekly(day, only=args.only)
    _print_report(day, results)
    return 1 if any(r.status == "error" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
