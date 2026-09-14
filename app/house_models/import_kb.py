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

from sqlalchemy.orm import Session

from app.house_models.data import HOUSE_MODEL_CARDS
from app.house_models.models import HouseModelCard

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

    db.commit()
    return created
