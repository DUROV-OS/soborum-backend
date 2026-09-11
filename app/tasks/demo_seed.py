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
from app.tasks.service import create_task
from app.users.models import User, UserModuleAccess, UserRole

# (full_name, email slug, modules, [task title, ...])
_EMPLOYEES: list[tuple[str, str, list[Module], list[str]]] = [
    (
        "Мария Кузнецова", "kuznecova", [Module.TASKS, Module.PRODUCTION],
        [
            "Проверить остаток бруса на складе перед закладкой модуля",
            "Согласовать смету по доп. комплектации с клиентом",
            "Обновить техкарту модуля после замены поставщика утеплителя",
        ],
    ),
    (
        "Дмитрий Соколов", "sokolov", [Module.TASKS, Module.INSTALLATION],
        [
            "Подготовить бригаду к выезду на монтаж",
            "Составить акт приёмки после монтажа",
            "Проработать замечания по итогам монтажа",
        ],
    ),
    (
        "Елена Морозова", "morozova", [Module.TASKS, Module.ACCOUNTING],
        [
            "Свести реестр движения денег за неделю",
            "Согласовать оплату поставщику метизов",
        ],
    ),
    (
        "Игорь Волков", "volkov", [Module.TASKS, Module.WAREHOUSE],
        [
            "Оприходовать поставку доски на склад",
            "Провести инвентаризацию остатков утеплителя",
        ],
    ),
    (
        "Анна Белова", "belova", [Module.TASKS, Module.MARKETING, Module.ACCOUNTING],
        [
            "Собрать контент-план на следующую неделю",
            "Опубликовать отчёт по выставке",
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

    for _full_name, slug, _modules, titles in _EMPLOYEES:
        employee = created_users[slug]
        for title in titles:
            create_task(db, title=title, assignee_ids=[employee.id])

    db.commit()
    return len(created_users)
