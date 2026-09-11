"""Демо-сидер: мокнутые клиенты, чтобы блок «Актуальное» на «Пульсе»
(`app.dashboard.aktualnoe`) было на что смотреть локально/при демонстрации —
он строится по активности циклов клиентов, а на пустой базе (после `0024`)
клиентов нет вовсе. Идемпотентно (skip, если в `clients` уже есть хоть одна
строка) и не запускается в prod. Задача 0025.

По образцу `app.board.seed.ensure_seed` / `app.tasks.demo_seed` (`0024`).
"""

from sqlalchemy.orm import Session

from app.clients.models import Client
from app.clients.schemas import ClientCreate
from app.clients.service import add_note, create_client
from app.core.config import settings
from app.users.models import User, UserRole

# (full_name, phone, email, note or None)
_CLIENTS: list[tuple[str, str, str, str | None]] = [
    (
        "Сергей Никитин", "+79161234501", "demo.nikitin@durov.local",
        "Заинтересован в доме 120 м², просил прислать типовые планировки.",
    ),
    (
        "Ольга Панова", "+79161234502", "demo.panova@durov.local",
        "Обсудили бюджет, ждёт согласования сметы от инженера.",
    ),
    ("Виктор Громов", "+79161234503", "demo.gromov@durov.local", None),
]


def ensure_demo_clients_seed(db: Session) -> int:
    """Возвращает число созданных демо-клиентов (0, если сидер уже
    срабатывал раньше, в базе уже есть свои клиенты, или мы в prod)."""
    if settings.is_prod:
        return 0
    if db.query(Client).first() is not None:
        return 0

    admin = db.query(User).filter(User.role == UserRole.ADMIN).order_by(User.id).first()

    created = 0
    for full_name, phone, email, note in _CLIENTS:
        client = create_client(db, ClientCreate(full_name=full_name, phone=phone, email=email))
        if note and admin is not None:
            add_note(db, client, author_id=admin.id, text=note)
        created += 1

    db.commit()
    return created
