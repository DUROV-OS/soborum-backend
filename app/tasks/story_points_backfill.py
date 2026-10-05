"""Фоновая оценка сторипоинтов (0070-d). Задача создаётся без оценки
(story_points IS NULL), а этот поток раз в `story_points_interval_seconds`
берёт порцию таких задач и оценивает их через app.ai.story_points. Так же
подхватываются задачи, заведённые до появления поля, — отдельный разовый
backfill после выката не нужен.

Почему не при создании: разворачивание производства из КР
(app.production.stage_plan) создаёт десятки-сотни задач в одном запросе
перехода клиента на «постоплату», и синхронный вызов ИИ на каждую растягивал
бы этот запрос на минуты.

Ручной прогон всей очереди (например, локально):

    python3 -c "
    from app.db.session import SessionLocal
    from app.tasks.story_points_backfill import backfill_missing
    with SessionLocal() as db:
        print(backfill_missing(db))
    "
"""

from __future__ import annotations

import logging
import threading
import time

from sqlalchemy.orm import Session

from app.ai import story_points as ai_story_points
from app.core.config import settings
from app.db.session import SessionLocal
from app.tasks.models import Task
from app.tasks.service import _section_hint

log = logging.getLogger("app.tasks.story_points")
_started = False


def estimate_pending(db: Session, limit: int) -> tuple[int, int]:
    """Оценивает до `limit` задач без очков, новые первыми. Коммитит после
    каждой задачи — долгая порция не держит транзакцию, а прогресс не
    теряется при сбое. Задача, которую ИИ не оценил (недоступен, ответ вне
    шкалы), остаётся NULL — следующий прогон попробует снова. Без ключа
    активного провайдера ничего не делает. Возвращает (оценено, просмотрено)."""
    if not settings.llm_configured:
        return 0, 0
    tasks = (
        db.query(Task)
        .filter(Task.story_points.is_(None))
        .order_by(Task.id.desc())
        .limit(limit)
        .all()
    )
    updated = 0
    for task in tasks:
        points = ai_story_points.estimate_story_points(
            task.title, task.description, _section_hint(task.block_id, task.link_type)
        )
        if points is None:
            continue
        task.story_points = points
        db.commit()
        updated += 1
    return updated, len(tasks)


def backfill_missing(db: Session) -> int:
    """Оценить всю очередь порциями, пока порция что-то оценивает.
    Возвращает число оценённых задач."""
    total = 0
    batch = max(1, settings.story_points_batch_size)
    while True:
        updated, seen = estimate_pending(db, batch)
        total += updated
        if updated == 0 or seen < batch:
            return total


def start_story_points_loop() -> None:
    global _started
    if _started or not settings.story_points_autorun:
        return
    _started = True
    thread = threading.Thread(target=_run, name="story-points", daemon=True)
    thread.start()
    log.info("Оценка сторипоинтов: каждые %s с, порция %s", settings.story_points_interval_seconds, settings.story_points_batch_size)


def _run() -> None:
    time.sleep(30)
    while True:
        db = SessionLocal()
        try:
            updated, seen = estimate_pending(db, max(1, settings.story_points_batch_size))
            if updated:
                log.info("Сторипоинты: оценено %s из %s задач", updated, seen)
        except Exception:
            db.rollback()
            log.exception("Оценка сторипоинтов не прошла")
        finally:
            db.close()
        time.sleep(max(60, settings.story_points_interval_seconds))
