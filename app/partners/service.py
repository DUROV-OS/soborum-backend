from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.clients.service import _phone_tail
from app.partners.models import Partner, PartnerCategory, PartnerNote
from app.partners.schemas import PartnerCreate, PartnerUpdate


def get_partner_or_404(db: Session, partner_id: int) -> Partner:
    partner = db.get(Partner, partner_id)
    if not partner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Партнёр не найден")
    return partner


def create_partner(db: Session, payload: PartnerCreate, created_by_id: int | None) -> Partner:
    data = payload.model_dump()
    partner = Partner(**data, created_by_id=created_by_id)
    db.add(partner)
    db.flush()
    return partner


def update_partner(db: Session, partner: Partner, payload: PartnerUpdate) -> Partner:
    for field, value in payload.model_dump(exclude_unset=True).items():
        if value is None and field in ("category", "name", "city", "contacts"):
            # Обязательное поле явно передано как null — не стираем, а
            # оставляем как было: иначе партнёр остался бы без города/имени.
            continue
        setattr(partner, field, value)
    db.flush()
    return partner


def delete_partner(db: Session, partner: Partner) -> None:
    db.delete(partner)
    db.flush()


def list_partners(
    db: Session,
    category: PartnerCategory | None = None,
    city: str | None = None,
    search: str | None = None,
) -> list[Partner]:
    """Отбор по городу и поиску — в Python, как search_clients в разделе
    «Клиенты»: регистронезависимое сравнение кириллицы в SQL ведёт себя
    по-разному в PostgreSQL и SQLite (на котором гоняются тесты)."""
    query = db.query(Partner)
    if category is not None:
        query = query.filter(Partner.category == category)
    partners = query.order_by(Partner.id.desc()).all()

    city_key = (city or "").strip().casefold()
    if city_key:
        partners = [p for p in partners if p.city.strip().casefold() == city_key]

    text = (search or "").strip()
    if not text:
        return partners
    lowered = text.casefold()
    digits = _phone_tail("".join(ch for ch in text if ch.isdigit()))
    found = []
    for partner in partners:
        if lowered in partner.name.casefold() or lowered in (partner.organization or "").casefold():
            found.append(partner)
            continue
        phone_digits = _phone_tail("".join(ch for ch in (partner.phone or "") if ch.isdigit()))
        if digits and phone_digits.endswith(digits):
            found.append(partner)
    return found


def list_cities(db: Session) -> list[str]:
    """Города из базы для фильтра: без дублей по регистру («москва» и «Москва»
    — один город, показывается написание первого заведённого партнёра)."""
    seen: dict[str, str] = {}
    for (city,) in db.query(Partner.city).order_by(Partner.id).all():
        seen.setdefault(city.strip().casefold(), city.strip())
    return sorted(seen.values(), key=str.casefold)


def add_note(db: Session, partner: Partner, author_id: int, text: str) -> PartnerNote:
    note = PartnerNote(partner_id=partner.id, author_id=author_id, text=text)
    db.add(note)
    db.flush()
    return note


def delete_note(db: Session, note: PartnerNote) -> None:
    db.delete(note)
    db.flush()
