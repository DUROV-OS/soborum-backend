"""Демо-сидер: мокнутые клиенты, чтобы блок «Актуальное» на «Пульсе»
(`app.dashboard.aktualnoe`) было на что смотреть локально/при демонстрации —
он строится по активности циклов клиентов, а на пустой базе (после `0024`)
клиентов нет вовсе. Идемпотентно (skip, если в `clients` уже есть хоть одна
строка) и не запускается в prod. Задача 0025.

По образцу `app.board.seed.ensure_seed` / `app.tasks.demo_seed` (`0024`).
"""

import os

from sqlalchemy.orm import Session

from app.clients.models import Client, ClientStage
from app.clients.schemas import ClientCreate
from app.clients.service import add_note, create_client, set_ar_file, set_kr_file
from app.common.files import FilePurpose, save_bytes_file
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

# Реальные шаблоны, присланные Арсением для теста «Главной» производства
# (0065-d) — не сгенерированные заглушки. «Проект дома» намеренно не
# заполняется: среди присланного нет отдельного файла с таким назначением,
# а поле опционально (0061) — честный пробел лучше подмены.
_ASSETS_DIR = os.path.join(os.path.dirname(__file__), "assets", "demo_documents")
_AR_SAMPLE = os.path.join(_ASSETS_DIR, "ar_sample.pdf")
_KR_SAMPLE = os.path.join(_ASSETS_DIR, "kr_sample.pdf")


def ensure_demo_clients_seed(db: Session) -> int:
    """Возвращает число созданных демо-клиентов (0, если сидер уже
    срабатывал раньше, в базе уже есть свои клиенты, или мы в prod)."""
    if settings.is_prod:
        return 0
    if db.query(Client).first() is not None:
        return 0

    admin = db.query(User).filter(User.role == UserRole.ADMIN).order_by(User.id).first()

    created = 0
    first_client: Client | None = None
    for full_name, phone, email, note in _CLIENTS:
        client = create_client(db, ClientCreate(full_name=full_name, phone=phone, email=email))
        if first_client is None:
            first_client = client
        if note and admin is not None:
            add_note(db, client, author_id=admin.id, text=note)
        created += 1

    if first_client is not None and admin is not None:
        _attach_demo_documents(db, first_client, admin)

    db.commit()
    return created


def _attach_demo_documents(db: Session, client: Client, uploaded_by: User) -> None:
    """АР и КР — реальные закоммиченные PDF (см. `assets/demo_documents/`),
    не сгенерированные заглушки, по образцу `app.house_models.import_kb`
    (реальные фото/планировки, закоммиченные как есть)."""
    if not (os.path.exists(_AR_SAMPLE) and os.path.exists(_KR_SAMPLE)):
        return

    with open(_AR_SAMPLE, "rb") as f:
        ar_asset = save_bytes_file(
            db, "АР_ИП Рура от 29.01.pdf", "application/pdf", f.read(),
            FilePurpose.ARCHITECTURAL_DECISIONS, uploaded_by,
        )
    with open(_KR_SAMPLE, "rb") as f:
        kr_asset = save_bytes_file(
            db, "КР_1 блок6.pdf", "application/pdf", f.read(),
            FilePurpose.CONSTRUCTIVE_DECISIONS, uploaded_by,
        )
    set_ar_file(db, client, ar_asset.id)
    set_kr_file(db, client, kr_asset.id)
    # Демо-клиент уже «в производстве» — документы реалистично зафиксированы
    # (см. app.production.demo_seed, которая переводит именно этот цикл в
    # CycleStatus.PRODUCTION).
    client.stage = ClientStage.POSTPAYMENT
    db.flush()
