from copy import deepcopy
from datetime import date, datetime, timedelta, timezone

import pytest

from app.common.module_access import AccessLevel, Module
from app.crm import service
from app.crm.models import CRMEntity, CRMEvent
from app.moysklad.models import SourceRecord


IMPORTED = datetime(2026, 11, 4, tzinfo=timezone.utc)
SOURCE_TYPES = {"deal": "leads", "inquiry": "leads/unsorted", "contact": "contacts", "company": "companies", "task": "tasks"}


def row(db, kind, data=None, *, source_id=None, created=IMPORTED, name="Offer", archived=False):
    result = CRMEntity(kind=kind, name=name, payload=data or {}, source_id=source_id, created_at=created, archived=archived)
    db.add(result)
    db.flush()
    return result


def source(db, entity, created):
    db.add(SourceRecord(entity_type="amocrm/" + SOURCE_TYPES[entity.kind], source_id=entity.source_id,
                        title="Source", search_text="", payload={"created_at": created}, imported_at=IMPORTED))
    db.flush()


def board(db):
    pipeline = row(db, "pipeline", name="Допродажная воронка")
    stage = row(db, "stage", {"pipeline_id": pipeline.id, "outcome": "open", "sort": 10})
    return pipeline, stage


def column(result, stage):
    return next(item for item in result["columns"] if item["stage_id"] == stage.id)


def test_original_dates_sort_all_pages_before_pagination_and_never_use_import_time(db, make_user, api):
    pipeline, stage = board(db)
    expected = []
    for index in range(126):
        # IDs deliberately run opposite to the chronology.
        created = datetime(2023, 1, 1, tzinfo=timezone.utc) + timedelta(days=125 - index)
        deal = row(db, "deal", {"pipeline_id": pipeline.id, "stage_id": stage.id, "amount": 10}, source_id=str(index + 1))
        source(db, deal, int(created.timestamp()))
        expected.append((created, deal.id))
    unknown = row(db, "deal", {"pipeline_id": pipeline.id, "stage_id": stage.id}, source_id="missing-original")
    tied = row(db, "deal", {"pipeline_id": pipeline.id, "stage_id": stage.id}, source_id="tied")
    source(db, tied, int(expected[0][0].timestamp()))
    expected.append((expected[0][0], tied.id))
    db.commit()
    before = [(entry.id, entry.version, deepcopy(entry.payload), entry.created_at) for entry in db.query(CRMEntity)]
    event_count = db.query(CRMEvent).count()
    client = api(make_user(Module.CLIENTS, level=AccessLevel.VIEW))
    pages = [client.get("/api/crm/entries", params={"kind": "deal", "limit": 100, "offset": offset}).json() for offset in (0, 100)]
    assert [page["total"] for page in pages] == [128, 128]
    entries = [entry for page in pages for entry in page["items"]]
    assert [entry["id"] for entry in entries] == [id for _, id in sorted(expected, reverse=True)] + [unknown.id]
    assert entries[0]["source_created_at"] == expected[0][0].isoformat()
    assert entries[0]["source_created_at_kind"] == "amocrm"
    assert entries[-1]["source_created_at"] is None and entries[-1]["source_created_at_kind"] == "unknown"
    detail = client.get(f"/api/crm/entries/{tied.id}").json()
    assert detail["source_created_at"] == entries[0]["source_created_at"]
    assert column(client.get("/api/crm/kanban-summary", params={"date_from": "2023-01-01", "date_to": "2023-05-31"}).json(), stage)["count"] == 127
    assert column(client.get("/api/crm/kanban-summary", params={"date_unknown": "true"}).json(), stage)["count"] == 1
    assert [(entry.id, entry.version, entry.payload, entry.created_at) for entry in db.query(CRMEntity)] == before
    assert db.query(CRMEvent).count() == event_count


def test_moscow_inclusive_day_boundaries_share_owner_search_pipeline_and_summary_filters(db, make_user, api):
    owner = make_user(Module.CLIENTS, level=AccessLevel.VIEW)
    other = make_user(Module.CLIENTS)
    pipeline, stage = board(db)
    data = {"pipeline_id": pipeline.id, "stage_id": stage.id, "amount": 10, "responsible_user_id": owner.id}
    instants = ["2026-10-31T20:59:59+00:00", "2026-10-31T21:00:00+00:00", "2026-11-01T20:59:59+00:00", "2026-11-01T21:00:00+00:00"]
    ids = []
    for index, instant in enumerate(instants):
        deal = row(db, "deal", data, source_id=f"boundary{index}", name="Offer%_")
        source(db, deal, datetime.fromisoformat(instant).timestamp())
        ids.append(deal.id)
    for delta, changes in [("owner", {"responsible_user_id": other.id}), ("pipeline", {"pipeline_id": 999999})]:
        excluded = row(db, "deal", {**data, **changes}, source_id=delta, name="Offer%_")
        source(db, excluded, datetime.fromisoformat(instants[1]).timestamp())
    hidden = row(db, "deal", {**data, "is_unsorted": True, "amount": 70}, source_id="hidden")
    source(db, hidden, datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp())
    incoming = row(db, "inquiry", {"pipeline_id": pipeline.id, "deal_id": hidden.id}, source_id="incoming", name="Offer%_")
    source(db, incoming, datetime.fromisoformat(instants[1]).timestamp())
    archived = row(db, "deal", data, source_id="archived", name="Offer%_", archived=True)
    source(db, archived, datetime.fromisoformat(instants[1]).timestamp())
    db.commit()
    client = api(owner)
    filters = {"date_from": "2026-11-01", "date_to": "2026-11-01", "query": "offer%_", "owner_id": owner.id, "pipeline_id": pipeline.id}
    deals = client.get("/api/crm/entries", params={"kind": "deal", "stage_id": stage.id, **filters}).json()
    assert deals["total"] == 2 and [entry["id"] for entry in deals["items"]] == [ids[2], ids[1]]
    inquiries = client.get("/api/crm/entries", params={"kind": "inquiry", **filters}).json()
    assert inquiries["total"] == 1 and inquiries["items"][0]["id"] == incoming.id
    assert column(client.get("/api/crm/kanban-summary", params=filters).json(), stage) == {
        "pipeline_id": pipeline.id, "stage_id": stage.id, "count": 3, "unpriced_count": 0, "amounts": {"RUB": 90}}
    assert client.get("/api/crm/entries", params={"kind": "deal", "archive": "archived", **filters}).json()["items"][0]["id"] == archived.id


def test_inquiry_uses_own_source_then_linked_original_not_hidden_import_time(db):
    hidden = row(db, "deal", {"is_unsorted": True}, source_id="hidden")
    older = datetime(2024, 1, 1, tzinfo=timezone.utc)
    newer = datetime(2025, 1, 1, tzinfo=timezone.utc)
    source(db, hidden, older.timestamp())
    own = row(db, "inquiry", {"deal_id": hidden.id}, source_id="own")
    source(db, own, newer.timestamp())
    linked_fallback = row(db, "inquiry", {"deal_id": hidden.id}, source_id="missing-own")
    unknown_deal = row(db, "deal", {"is_unsorted": True}, source_id="missing-deal")
    unknown_inquiry = row(db, "inquiry", {"deal_id": unknown_deal.id}, source_id="missing-both")
    db.commit()
    assert service.out(own)["source_created_at"] == newer.isoformat()
    assert service.out(linked_fallback)["source_created_at"] == older.isoformat()
    assert service.out(linked_fallback)["source_created_at_kind"] == "amocrm"
    assert service.out(unknown_inquiry)["source_created_at"] is None
    dated = service.list_entries(db, "inquiry", date_from=date(2024, 1, 1), date_to=date(2024, 12, 31))
    assert [entry["id"] for entry in dated["items"]] == [linked_fallback.id]
    assert service.list_entries(db, "inquiry", date_unknown=True)["items"][0]["id"] == unknown_inquiry.id


def test_website_original_request_survives_acceptance_and_native_creation_is_valid(db, make_user):
    from app.clients.models import ClientLeadRequest
    from app.clients.service import intake_lead
    from app.clients.schemas import LeadIntakePayload
    from app.crm.schemas import Entry

    owner = make_user(admin=True)
    pipeline, stage = board(db)
    client, _ = intake_lead(db, LeadIntakePayload(name="Website", phone="89001112233"))
    request = db.query(ClientLeadRequest).filter_by(client_id=client.id).one()
    original = datetime(2024, 6, 1, 21, 30, tzinfo=timezone.utc)
    request.created_at = original
    inquiry = db.query(CRMEntity).filter_by(kind="inquiry").one()
    inquiry.created_at = IMPORTED
    db.commit()
    assert service.out(inquiry)["source_created_at"] == original.isoformat()
    assert service.out(inquiry)["source_created_at_kind"] == "website"
    accepted = service.accept_inquiry(db, inquiry, Entry(name="Accepted", data={"pipeline_id": pipeline.id, "stage_id": stage.id}), owner)
    assert accepted["source_created_at"] == original.isoformat()
    assert accepted["source_created_at_kind"] == "website"
    assert service.list_entries(db, "deal", date_from=date(2024, 6, 2), date_to=date(2024, 6, 2))["total"] == 1
    local = row(db, "deal", {"pipeline_id": pipeline.id, "stage_id": stage.id}, created=original)
    assert service.out(local)["source_created_at"] == original.isoformat()
    assert service.out(local)["source_created_at_kind"] == "local"


@pytest.mark.parametrize("invalid", [None, 0, -1, "2025-01-01", "1735689600", {}, [], True, 253402300800])
def test_invalid_original_metadata_stays_unknown(db, invalid):
    imported = row(db, "deal", source_id="bad")
    source(db, imported, invalid)
    db.commit()
    result = service.list_entries(db, "deal", date_unknown=True)
    assert result["total"] == 1
    assert result["items"][0]["source_created_at"] is None
    assert result["items"][0]["source_created_at_kind"] == "unknown"
    assert service.list_entries(db, "deal", date_from=date(2026, 11, 4))["total"] == 0


def test_date_validation_and_permissions(db, make_user, api):
    viewer = api(make_user(Module.CLIENTS, level=AccessLevel.VIEW))
    for endpoint in ("/api/crm/entries", "/api/crm/kanban-summary"):
        for params in ({"date_from": "not-a-date"}, {"date_from": "2026-11-05", "date_to": "2026-11-04"},
                       {"date_unknown": True, "date_to": "2026-11-04"}, {"date_to": "9999-12-31"}):
            assert viewer.get(endpoint, params=params).status_code == 422
        assert viewer.get(endpoint, params={"date_unknown": True}).status_code == 200
    denied = api(make_user(Module.TASKS))
    assert denied.get("/api/crm/entries?date_from=2026-11-04").status_code == 403
    assert denied.get("/api/crm/kanban-summary?date_unknown=true").status_code == 403
