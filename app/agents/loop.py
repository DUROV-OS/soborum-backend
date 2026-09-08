"""Automatic company shift. Human does not press start."""

from __future__ import annotations

import logging
import threading
import time

from app.core.config import settings
from app.db.session import SessionLocal

log = logging.getLogger("app.agents.loop")
_started = False


def start_shift_loop() -> None:
    global _started
    if _started or not settings.agent_shift_autorun:
        return
    _started = True
    thread = threading.Thread(target=_run, name="agent-shift-loop", daemon=True)
    thread.start()
    log.info(
        "Смена компании сама: первая сверка сразу, дальше каждые %s с",
        settings.agent_shift_interval_seconds,
    )


def _run() -> None:
    time.sleep(2)
    while True:
        try:
            _tick_once()
        except Exception:
            log.exception("Тик смены не записался")
        time.sleep(max(30, settings.agent_shift_interval_seconds))


def _tick_once() -> None:
    from app.agents.service import maybe_tick_shift

    db = SessionLocal()
    try:
        shift = maybe_tick_shift(db)
        if shift is not None:
            log.info("Смена #%s, к человеку %s", shift.id, len(shift.approvals))
    finally:
        db.close()
