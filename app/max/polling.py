"""Long polling событий MAX-бота — только для локальной разработки (0082).

На проде события приходят webhook'ом (POST /api/max/webhook). Включается
``MAX_BOT_POLLING=true``; с активной webhook-подпиской MAX polling не отдаёт
ничего. marker держим в памяти процесса: после рестарта MAX отдаст то, что
не успели подтвердить, — ingest идемпотентен.
"""

from __future__ import annotations

import logging
import threading
import time

from app.core.config import settings
from app.db.session import SessionLocal

log = logging.getLogger("app.max.polling")
_started = False


def start_max_polling_loop() -> None:
    global _started
    if _started or not settings.max_bot_polling:
        return
    if not settings.max_bot_token:
        log.warning("MAX_BOT_POLLING включён, но MAX_BOT_TOKEN не задан — polling не запущен")
        return
    _started = True
    threading.Thread(target=_run, name="max-bot-polling", daemon=True).start()
    log.info("MAX-бот: long polling событий запущен")


def _run() -> None:
    from app.max import bot_api
    from app.max.ingest import handle_updates

    marker: int | None = None
    while True:
        try:
            data = bot_api.get_updates(marker=marker, timeout=30)
            updates = data.get("updates") or []
            if updates:
                db = SessionLocal()
                try:
                    handle_updates(db, updates)
                finally:
                    db.close()
                log.info("MAX-бот: принято событий %s", len(updates))
            marker = data.get("marker", marker)
        except Exception:  # noqa: BLE001
            log.exception("MAX-бот: сбой long polling, повтор через 10 с")
            time.sleep(10)
