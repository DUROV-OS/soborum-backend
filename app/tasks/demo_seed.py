"""Демо-сидер: мокнутые сотрудники, которым поставлены задачи, чтобы на
приёмке/при живой демонстрации было видно, как работает раздел «Задачи»
(`app.tasks.service`). Идемпотентно (skip, если в `users` уже есть хоть один
`role=WORKER`) и не запускается в prod — это данные для демонстрации, не для
боевой базы. Задача 0024.

По образцу `app.board.seed.ensure_seed` и `app.accounting.seed.ensure_accounting_seed`:
делает что-то ровно один раз после первого деплоя, no-op на каждом рестарте.
"""

from sqlalchemy.orm import Session

from app.common.module_access import Module
from app.core.config import settings
from app.core.security import hash_password
from app.tasks.models import TaskStatus
from app.tasks.service import create_task, set_status
from app.users.models import User, UserModuleAccess, UserRole

# (full_name, email slug, modules, [(task title, needs_review, progress), ...])
# progress: "done" — доводится до DONE; "in_progress" — доведена и оставлена
# в работе; "ready" — не тронута, остаётся как поставлена. Смесь статусов
# нужна, чтобы на демонстрации был виден весь спектр, а не только готовое.
_EMPLOYEES: list[tuple[str, str, list[Module], list[tuple[str, bool, str]]]] = [
    (
        "Мария Кузнецова", "kuznecova", [Module.TASKS, Module.PRODUCTION],
        [
            ("Проверить остаток бруса на складе перед закладкой модуля", False, "done"),
            ("Согласовать смету по доп. комплектации с клиентом", True, "done"),
            ("Обновить техкарту модуля после замены поставщика утеплителя", False, "in_progress"),
        ],
    ),
    (
        "Дмитрий Соколов", "sokolov", [Module.TASKS, Module.INSTALLATION],
        [
            ("Подготовить бригаду к выезду на монтаж", False, "done"),
            ("Составить акт приёмки после монтажа", True, "done"),
            ("Проработать замечания по итогам монтажа", False, "ready"),
        ],
    ),
    (
        "Елена Морозова", "morozova", [Module.TASKS, Module.ACCOUNTING],
        [
            ("Свести реестр движения денег за неделю", False, "done"),
            ("Согласовать оплату поставщику метизов", True, "in_progress"),
        ],
    ),
    (
        "Игорь Волков", "volkov", [Module.TASKS, Module.WAREHOUSE],
        [
            ("Оприходовать поставку доски на склад", False, "done"),
            ("Провести инвентаризацию остатков утеплителя", False, "in_progress"),
        ],
    ),
    (
        "Анна Белова", "belova", [Module.TASKS, Module.MARKETING, Module.ACCOUNTING],
        [
            ("Собрать контент-план на следующую неделю", False, "done"),
            ("Опубликовать отчёт по выставке", True, "ready"),
        ],
    ),
]


def ensure_demo_workforce_seed(db: Session) -> int:
    """Возвращает число созданных демо-сотрудников (0, если сидер уже
    срабатывал раньше или в базе уже есть свои сотрудники)."""
    if settings.is_prod:
        return 0
    if db.query(User).filter(User.role == UserRole.WORKER).first() is not None:
        return 0

    created_users: dict[str, User] = {}
    for full_name, slug, modules, _titles in _EMPLOYEES:
        user = User(
            email=f"demo.{slug}@durov.local",
            hashed_password=hash_password(f"demo-{slug}-2026"),
            full_name=full_name,
            role=UserRole.WORKER,
            is_active=True,
        )
        db.add(user)
        db.flush()
        for module in modules:
            db.add(UserModuleAccess(user_id=user.id, module=module))
        created_users[slug] = user
    db.flush()

    reviewer = created_users["kuznecova"]
    for _full_name, slug, _modules, tasks_spec in _EMPLOYEES:
        employee = created_users[slug]
        for title, needs_review, progress in tasks_spec:
            reviewer_ids = [reviewer.id] if needs_review and slug != "kuznecova" else []
            task = create_task(
                db, title=title, assignee_ids=[employee.id], reviewer_ids=reviewer_ids
            )
            if progress == "ready":
                continue
            task = set_status(db, task, TaskStatus.IN_PROGRESS, actor=employee)
            if progress == "in_progress":
                continue
            task = set_status(db, task, TaskStatus.IN_REVIEW, actor=employee)
            if task.status == TaskStatus.IN_REVIEW and task.reviewers:
                set_status(db, task, TaskStatus.DONE, actor=task.reviewers[0])

    db.commit()
    return len(created_users)
