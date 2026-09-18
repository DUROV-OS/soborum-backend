"""Разовая операция (0070-d): проставить сторипоинты задачам, заведённым до
появления этого поля (или до того, как ИИ был доступен в момент создания).
Не часть автоматического пути - запускается вручную один раз после выката
миграции на каждом окружении, по образцу `scripts/backlog_sync.py
migrate-all`:

    python3 -c "
    from app.db.session import SessionLocal
    from app.tasks.story_points_backfill import backfill_missing
    with SessionLocal() as db:
        print(backfill_missing(db))
    "
"""

from sqlalchemy.orm import Session

from app.ai import story_points as ai_story_points
from app.tasks.models import Task
from app.tasks.service import _section_hint


def backfill_missing(db: Session) -> int:
    """Оценивает все задачи с story_points IS NULL. Как и при создании -
    best-effort: задача, для которой ИИ недоступен/ошибся, остаётся NULL и
    будет подхвачена следующим запуском. Возвращает число обновлённых задач."""
    tasks = db.query(Task).filter(Task.story_points.is_(None)).all()
    updated = 0
    for task in tasks:
        points = ai_story_points.estimate_story_points(
            task.title, task.description, _section_hint(task.block_id, task.link_type)
        )
        if points is not None:
            task.story_points = points
            updated += 1
    db.commit()
    return updated
