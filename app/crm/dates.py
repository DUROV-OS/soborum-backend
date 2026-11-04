"""Original CRM timestamps; import timestamps are never creation evidence."""
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import case, extract, func, literal, select
from sqlalchemy.orm import aliased

from app.crm.models import CRMEntity
from app.moysklad.models import SourceRecord


SOURCE_TYPES = {"deal": "leads", "inquiry": "leads/unsorted", "contact": "contacts",
                "company": "companies", "task": "tasks"}
MOSCOW = ZoneInfo("Europe/Moscow")


def _raw_epoch(db, kind, entity):
    source_type = SOURCE_TYPES.get(kind)
    if source_type is None:
        return literal(None)
    raw = aliased(SourceRecord)
    value = raw.payload["created_at"].as_float()
    json_value = raw.payload["created_at"]
    if db.get_bind().dialect.name == "postgresql":
        numeric = func.json_typeof(json_value) == "number"
    else:
        numeric = func.json_type(raw.payload, "$.created_at").in_(["integer", "real"])
    # Guard the cast with JSON type checking: malformed imported metadata stays
    # unknown rather than failing a whole page or becoming 1970-01-01.
    valid = case((numeric, case(((value > 0) & (value < 253402300800), value), else_=None)), else_=None)
    return (select(valid).where(raw.entity_type == "amocrm/" + source_type, raw.source_id == entity.source_id)
            .correlate(entity).scalar_subquery())


def source_date_expressions(db, kind, entity=CRMEntity):
    """Return SQL epoch/origin expressions for filtering, ordering and output."""
    from app.clients.models import ClientLeadRequest

    raw = _raw_epoch(db, kind, entity)
    local = case((entity.source_id.is_(None), extract("epoch", entity.created_at)), else_=None)
    sources = []
    if kind in ("deal", "inquiry"):
        request = aliased(ClientLeadRequest)
        website = (select(extract("epoch", request.created_at)).where(
            request.id == entity.payload["client_request_id"].as_integer(),
            request.client_id == entity.payload["client_id"].as_integer(),
        ).correlate(entity).scalar_subquery())
        sources.append((website, literal("website")))
    sources.extend([(raw, literal("amocrm")), (local, literal("local"))])
    if kind == "inquiry":
        linked = aliased(CRMEntity)
        linked_epoch, linked_origin = source_date_expressions(db, "deal", linked)
        linked_id = func.coalesce(func.nullif(entity.payload["accepted_deal_id"].as_integer(), 0),
                                  entity.payload["deal_id"].as_integer())
        linked_date = (select(linked_epoch).where(linked.kind == "deal", linked.id == linked_id)
                       .correlate(entity).scalar_subquery())
        linked_kind = (select(linked_origin).where(linked.kind == "deal", linked.id == linked_id)
                       .correlate(entity).scalar_subquery())
        sources.append((linked_date, linked_kind))
    epoch = func.coalesce(*(value for value, _ in sources))
    origin = case(*((value.is_not(None), label) for value, label in sources), else_="unknown")
    return epoch, origin


def source_date_fields(epoch, origin):
    return {
        "source_created_at": datetime.fromtimestamp(float(epoch), timezone.utc).isoformat() if epoch is not None else None,
        "source_created_at_kind": origin if epoch is not None else "unknown",
    }


def date_bounds(date_from: date | None, date_to: date | None, date_unknown=False):
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(422, "Дата начала не может быть позже даты окончания")
    if date_unknown and (date_from is not None or date_to is not None):
        raise HTTPException(422, "Выберите диапазон дат или заявки без исходной даты")
    start = datetime.combine(date_from, time.min, MOSCOW).timestamp() if date_from else None
    # The inclusive local end day is an exclusive midnight on the following day.
    if date_to == date.max:
        raise HTTPException(422, "Выберите более раннюю дату окончания")
    end = datetime.combine(date_to + timedelta(days=1), time.min, MOSCOW).timestamp() if date_to else None
    return start, end
