"""Ежедневный оркестратор.

Порядок:
  1. Прогнать все задачи-сборщики из реестра (app/jobs/registry.py).
     Сейчас это только выгрузка Telegram-чата в базу знаний; дальше сюда
     добавятся другие источники — каждый отдельным ``@daily_job``.
  2. Отдельным шагом — саммари базы знаний за день (app/jobs/kb_summary.py).

Запуск (обычно из cron / systemd timer раз в сутки после полуночи):

    python -m app.jobs.daily                # за вчера в kb-таймзоне
    python -m app.jobs.daily --date 2026-09-05
    python -m app.jobs.daily --only telegram_ingest   # без шага саммари

Каждая задача изолирована: её исключение попадает в отчёт, но не мешает
остальным.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

from app.db.session import SessionLocal
from app.jobs.dates import iso, yesterday
from app.jobs.kb_summary import summarize_day
from app.jobs.registry import JobResult, iter_daily_jobs, run_job

# Импортируем ради побочного эффекта — регистрации задач в реестре.
from app.telegram import ingest as _tg_ingest  # noqa: F401

logger = logging.getLogger("app.jobs.daily")


def run_daily(day: date | None = None, *, only: str | None = None, skip_summary: bool = False) -> list[JobResult]:
    day = day or yesterday()
    results: list[JobResult] = []

    with SessionLocal() as db:
        for name, fn in iter_daily_jobs():
            if only and name != only:
                continue
            logger.info("→ задача %s за %s", name, iso(day))
            results.append(run_job(name, fn, db, day))

        if not skip_summary and (only is None or only == "kb_daily_summary"):
            logger.info("→ саммари базы знаний за %s", iso(day))
            try:
                results.append(summarize_day(db, day))
            except Exception as e:  # noqa: BLE001
                db.rollback()
                logger.exception("Саммари упало")
                results.append(JobResult(name="kb_daily_summary", status="error").done("error", str(e)))

    return results


def _print_report(day: date, results: list[JobResult]) -> None:
    print(f"\n=== Ежедневный прогон за {iso(day)} ===")
    for r in results:
        secs = (r.finished_at - r.started_at).total_seconds() if r.finished_at else 0.0
        print(f"[{r.status.upper():7}] {r.name} ({secs:.1f}s) — {r.detail.strip()[:500]}")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Ежедневный оркестратор Durov OS")
    parser.add_argument("--date", help="ГГГГ-ММ-ДД; по умолчанию вчера в kb-таймзоне")
    parser.add_argument("--only", help="запустить только задачу с этим именем")
    parser.add_argument("--skip-summary", action="store_true", help="без шага саммари базы знаний")
    args = parser.parse_args(argv)

    day = date.fromisoformat(args.date) if args.date else yesterday()
    results = run_daily(day, only=args.only, skip_summary=args.skip_summary)
    _print_report(day, results)
    return 1 if any(r.status == "error" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
