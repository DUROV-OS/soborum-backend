"""Импорт каталога типовых проектов домов из базы знаний (задача 0043-a).

В отличие от `app.clients.demo_seed` и соседей, это **не** демо-сид уровня
0033: датасет (`app.house_models.data.HOUSE_MODEL_CARDS`) — реальный каталог
компании, а не синтетика для localhost, поэтому здесь нет проверки
`settings.is_prod` — карточки должны появляться во всех окружениях, включая
прод.

Идемпотентно через upsert по `key`: при повторном запуске (в т.ч. после
редактирования датасета — «дозагрузка при появлении новых карточек», см.
спецификацию задачи) существующие карточки обновляются полями из датасета,
а не дублируются.
"""

from pathlib import Path

from sqlalchemy.orm import Session

from app.common.files import FilePurpose, save_bytes_file
from app.house_models.data import HOUSE_MODEL_CARDS
from app.house_models.models import HouseModelCard
from app.users.models import User, UserRole

_ASSETS_DIR = Path(__file__).parent / "assets" / "planirovki"
_CONTENT_TYPE = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}

_FIELDS = [
    "title",
    "kind",
    "series",
    "area_footprint_m2",
    "area_total_m2",
    "price_site_rub",
    "deal_amount_rub",
    "client_name",
    "confirmation",
    "confirmation_label",
    "source_note_path",
    "characteristics_md",
    "planning_md",
    "configurations_md",
    "modules_md",
    "economics_md",
    "production_experience_md",
    "deals_without_pz_md",
    "files_md",
    "open_questions_md",
    "notes_md",
]


def _attach_planning_image(db: Session, card: HouseModelCard, asset_filename: str) -> None:
    """Привязывает забандленное с кодом изображение планировки (скачано один
    раз с durov.house, см. задачу 0043-c — приложение само к durov.house
    больше не обращается). Не трогает уже привязанную карточку — иначе каждый
    перезапуск плодил бы новый FileAsset и новый файл на диске."""
    if card.planning_image_id is not None:
        return
    path = _ASSETS_DIR / asset_filename
    admin = db.query(User).filter(User.role == UserRole.ADMIN).order_by(User.id).first()
    if admin is None:
        return
    data = path.read_bytes()
    content_type = _CONTENT_TYPE.get(path.suffix.lower(), "application/octet-stream")
    asset = save_bytes_file(db, asset_filename, content_type, data, FilePurpose.HOUSE_MODEL_PLANNING, admin)
    card.planning_image_id = asset.id


def ensure_house_models_seed(db: Session) -> int:
    """Возвращает число вновь созданных карточек (0 при повторном запуске
    без изменений в датасете — существующие карточки лишь обновляются)."""
    created = 0
    for entry in HOUSE_MODEL_CARDS:
        card = db.query(HouseModelCard).filter(HouseModelCard.key == entry["key"]).first()
        if card is None:
            card = HouseModelCard(key=entry["key"])
            db.add(card)
            created += 1
        for field in _FIELDS:
            setattr(card, field, entry[field])
        if entry["planning_image_asset"]:
            _attach_planning_image(db, card, entry["planning_image_asset"])

    db.commit()
    return created
