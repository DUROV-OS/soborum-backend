"""Демо-сидер: мокнутое производство (дом, модуль, просроченная задача), чтобы
вкладку «Главная» одного производства (0065) было на чём проверить локально —
«Требует внимания» и «Сроки» нуждаются в реальном сигнале, а «Актуальное» и
документы клиента — в цикле, который уже дошёл до производства. Идемпотентно
(skip, если в `productions` уже есть хоть одна строка) и не запускается в
prod. Задача 0065-d.

Переводит цикл демо-клиента, у которого уже есть АР/КР
(`app.clients.demo_seed._attach_demo_documents`), напрямую в
`CycleStatus.PRODUCTION` — в обход полного конвейера стадий, как и
остальные `demo_seed` (моделирование готового состояния для демонстрации,
а не проверка бизнес-процесса; см. исторический прецедент `0040`,
коммит `dfb84d1` в ветке `feat/warehouse/demo-seed-production-marketing`,
которая не была влита в `main`).
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.clients.models import Client
from app.core.config import settings
from app.cycle.models import Cycle, CycleStatus
from app.production.models import Production, ProductionModule
from app.tasks.models import Task, TaskStatus
from app.users.models import User, UserRole


def ensure_demo_production_seed(db: Session) -> int:
    """Возвращает 1, если демо-производство создано (0 — в prod, если
    производство уже есть, или нет демо-клиента с документами для привязки)."""
    if settings.is_prod:
        return 0
    if db.query(Production).first() is not None:
        return 0

    client = db.query(Client).filter(Client.ar_file_id.isnot(None)).order_by(Client.id).first()
    if client is None:
        return 0

    admin = db.query(User).filter(User.role == UserRole.ADMIN).order_by(User.id).first()
    if admin is None:
        return 0

    cycle = db.get(Cycle, client.cycle_id)
    cycle.status = CycleStatus.PRODUCTION
    db.flush()

    production = Production(cycle_id=cycle.id, house_index=1, name="Дом")
    db.add(production)
    db.flush()

    module = ProductionModule(
        production_id=production.id, name="Каркас и обшивка",
        description="Несущий каркас модуля и внешняя обшивка",
    )
    db.add(module)
    db.flush()

    # Реалистичный сигнал для «Требует внимания» / «Сроки» на «Главной» —
    # просроченная задача этого модуля.
    task = Task(
        title="Смонтировать каркас модуля",
        module_id=module.id,
        status=TaskStatus.READY,
        deadline=datetime.now(timezone.utc) - timedelta(days=3),
    )
    task.assignees = [admin]
    db.add(task)

    db.commit()
    return 1
