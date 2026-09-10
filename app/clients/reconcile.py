"""Сверка задач смены стадии клиента с реальностью.

Инвариант: у каждого клиента, не находящегося на последней стадии, есть ровно
одна открытая задача «перевести на следующую стадию», и её стадия совпадает с
текущей стадией клиента.

Инвариант нарушают клиенты, чья стадия выставлена в обход сервиса
(`sources/seed_*.sql`, ручные правки БД, старый код без task-sync) — у них
задачи просто нет. Эта сверка чинит такое: создаёт недостающие задачи,
закрывает задачи под уже пройденную стадию и лишние дубликаты, закрывает
задачи у клиентов на последней стадии.

Запускается фоновым потоком: сразу после старта приложения и дальше раз в час
(см. `app/main.py`). Плюс ручной прогон — `POST /api/clients/reconcile-stage-tasks`.
"""

from __future__ import annotations

import logging
import threading
import time

from sqlalchemy.orm import Session

from app.clients.models import Client
from app.clients.service import _next_stage, _open_stage_tasks, ensure_stage_transition_task
from app.core.config import settings
from app.db.session import SessionLocal
from app.tasks import service as task_service

log = logging.getLogger("app.clients.reconcile")
_started = False


def reconcile_client_stage_tasks(db: Session) -> dict:
    """Приводит задачи смены стадии к инварианту. Не коммитит — вызывающий сам."""
    created = 0
    closed_stale = 0
    closed_final = 0
    deduped = 0

    clients = db.query(Client).all()
    for client in clients:
        open_tasks = _open_stage_tasks(db, client.id)

        if _next_stage(client.stage) is None:
            # последняя стадия — открытых задач быть не должно
            for task in open_tasks:
                task_service.force_close(db, task)
                closed_final += 1
            continue

        # «под текущую стадию» = стадия в link_meta совпадает, либо неизвестна
        # (старые задачи без link_meta — не трогаем, считаем валидными)
        keep = [t for t in open_tasks if (t.link_meta or {}).get("stage") in (None, client.stage.value)]
        for task in open_tasks:
            if task not in keep:
                task_service.force_close(db, task)
                closed_stale += 1

        if not keep:
            ensure_stage_transition_task(db, client)
            created += 1
        elif len(keep) > 1:
            # _open_stage_tasks отсортирован по id убыв. — оставляем самую свежую
            for task in keep[1:]:
                task_service.force_close(db, task)
                deduped += 1

    return {
        "checked": len(clients),
        "created": created,
        "closed_stale": closed_stale,
        "closed_final": closed_final,
        "deduped": deduped,
    }


def run_once() -> dict:
    db = SessionLocal()
    try:
        report = reconcile_client_stage_tasks(db)
        db.commit()
        return report
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def start_stage_task_reconcile_loop() -> None:
    global _started
    if _started or not settings.stage_task_reconcile_autorun:
        return
    _started = True
    thread = threading.Thread(target=_run, name="client-stage-reconcile", daemon=True)
    thread.start()
    log.info(
        "Сверка задач стадий клиентов: сразу после старта, дальше каждые %s с",
        settings.stage_task_reconcile_interval_seconds,
    )


def _run() -> None:
    time.sleep(5)
    while True:
        try:
            report = run_once()
            if report["created"] or report["closed_stale"] or report["closed_final"] or report["deduped"]:
                log.info(
                    "Сверка: клиентов %s, создано %s, закрыто устаревших %s, "
                    "закрыто на финале %s, дедуп %s",
                    report["checked"],
                    report["created"],
                    report["closed_stale"],
                    report["closed_final"],
                    report["deduped"],
                )
        except Exception:
            log.exception("Сверка задач стадий не прошла")
        time.sleep(max(60, settings.stage_task_reconcile_interval_seconds))
