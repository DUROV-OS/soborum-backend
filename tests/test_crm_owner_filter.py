from copy import deepcopy

import pytest

from app.common.module_access import AccessLevel, Module
from app.crm import service
from app.crm.models import CRMEntity, CRMEvent


def row(db, kind, name, data=None, archived=False):
    result = CRMEntity(kind=kind, name=name, payload=data or {}, archived=archived)
    db.add(result)
    db.flush()
    return result


def board(db, name="Допродажная воронка"):
    pipeline = row(db, "pipeline", name)
    first = row(db, "stage", "Новая заявка", {"pipeline_id": pipeline.id, "sort": 10, "outcome": "open"})
    next_ = row(db, "stage", "В работе", {"pipeline_id": pipeline.id, "sort": 20, "outcome": "open"})
    return pipeline, first, next_


def column(result, stage):
    return next(item for item in result["columns"] if item["stage_id"] == stage.id)


def test_owner_filters_all_pages_and_summary_without_writes(db, make_user, api):
    owner = make_user(Module.CLIENTS, level=AccessLevel.VIEW)
    other = make_user(Module.CLIENTS)
    pipeline, first, _ = board(db)
    base = {"pipeline_id": pipeline.id, "stage_id": first.id, "responsible_user_id": owner.id}
    deal_ids, inquiry_ids = set(), set()
    for index in range(123):
        deal_ids.add(row(db, "deal", f"Deal {index}", {**base, "amount": 10}).id)
    for index in range(105):
        hidden = row(db, "deal", f"Hidden {index}", {**base, "amount": 20, "is_unsorted": True})
        inquiry_ids.add(row(db, "inquiry", f"Inquiry {index}", {"pipeline_id": pipeline.id, "deal_id": hidden.id}).id)
    row(db, "deal", "Other owner", {**base, "amount": 500, "responsible_user_id": other.id})
    other_hidden = row(db, "deal", "Other hidden", {**base, "amount": 700, "responsible_user_id": other.id, "is_unsorted": True})
    row(db, "inquiry", "Other inquiry", {"deal_id": other_hidden.id})
    row(db, "deal", "No owner", {**base, "amount": 30, "responsible_user_id": None})
    row(db, "inquiry", "Unassigned website")
    db.commit()
    before = [(entry.id, entry.version, deepcopy(entry.payload)) for entry in db.query(CRMEntity)]
    events = db.query(CRMEvent).count()
    client = api(owner)

    for kind, expected_ids in [("deal", deal_ids), ("inquiry", inquiry_ids)]:
        pages = [client.get("/api/crm/entries", params={
            "kind": kind, "owner_id": owner.id, "limit": 100, "offset": offset,
        }).json() for offset in (0, 100, 200)]
        assert [page["total"] for page in pages] == [len(expected_ids)] * 3
        entries = [entry for page in pages for entry in page["items"]]
        assert len(entries) == len(expected_ids)
        assert {entry["id"] for entry in entries} == expected_ids
        assert all(entry["effective_owner_id"] == owner.id for entry in entries)
        assert not pages[-1]["items"]

    result = client.get("/api/crm/kanban-summary", params={"owner_id": owner.id}).json()
    assert column(result, first) == {"pipeline_id": pipeline.id, "stage_id": first.id,
        "count": 228, "amounts": {"RUB": 3330}, "unpriced_count": 0}
    assert column(service.kanban_summary(db, owner_id=other.id), first)["amounts"] == {"RUB": 1200}
    assert column(service.kanban_summary(db, owner_id=0), first) == {
        "pipeline_id": pipeline.id, "stage_id": first.id, "count": 2,
        "amounts": {"RUB": 30}, "unpriced_count": 1,
    }
    assert column(service.kanban_summary(db), first)["count"] == 232
    assert [(entry.id, entry.version, entry.payload) for entry in db.query(CRMEntity)] == before
    assert db.query(CRMEvent).count() == events


def test_inquiry_owner_matches_linked_record_card_including_unassigned(db, make_user):
    first = make_user(Module.CLIENTS)
    second = make_user(Module.CLIENTS)
    owned = row(db, "deal", "First", {"responsible_user_id": first.id, "is_unsorted": True})
    accepted = row(db, "deal", "Accepted", {"responsible_user_id": second.id})
    no_owner = row(db, "deal", "No owner", {"responsible_user_id": None, "is_unsorted": True})
    not_deal = row(db, "contact", "Wrong linked kind", {"responsible_user_id": first.id})
    cases = [
        ({"deal_id": owned.id, "responsible_user_id": second.id}, first.id),
        ({"deal_id": owned.id, "accepted_deal_id": accepted.id}, second.id),
        ({"deal_id": owned.id, "accepted_deal_id": 0}, first.id),
        ({"deal_id": owned.id, "accepted_deal_id": 999999}, None),
        ({"deal_id": no_owner.id, "responsible_user_id": second.id}, second.id),
        ({"deal_id": not_deal.id}, None),
        ({"deal_id": 999999}, None),
        ({"responsible_user_id": first.id}, first.id),
        ({"responsible_user_id": None}, None),
        ({"responsible_user_id": 0}, None),
        ({}, None),
    ]
    expected = {}
    for index, (data, effective_owner) in enumerate(cases):
        inquiry = row(db, "inquiry", f"Incoming {index}", data, archived=bool(data.get("accepted_deal_id")))
        expected[inquiry.id] = effective_owner
    website = row(db, "inquiry", "Website", {"manager_id": first.id})
    row(db, "task", "Assigned followup", {"entity_id": website.id, "responsible_user_id": first.id})
    expected[website.id] = None
    db.commit()

    all_ = service.list_entries(db, "inquiry", archive="all")
    assert {entry["id"]: entry["effective_owner_id"] for entry in all_["items"]} == expected
    for owner_id in (0, first.id, second.id):
        result = service.list_entries(db, "inquiry", archive="all", owner_id=owner_id)
        ids = {key for key, value in expected.items() if (value or 0) == owner_id}
        assert result["total"] == len(ids)
        assert {entry["id"] for entry in result["items"]} == ids
    assert "responsible_user_id" not in website.payload


def test_owner_combines_with_query_archive_pipeline_and_stage(db, make_user):
    owner = make_user(Module.CLIENTS)
    other = make_user(Module.CLIENTS)
    pipeline, first, next_ = board(db)
    other_pipeline, other_first, _ = board(db, "Другая воронка")
    base = {"pipeline_id": pipeline.id, "stage_id": first.id, "responsible_user_id": owner.id}
    target = row(db, "deal", "Offer%_", {**base, "amount": 100})
    row(db, "deal", "Offer%_ next", {**base, "stage_id": next_.id, "amount": 150})
    row(db, "deal", "Wrong owner Offer%_", {**base, "responsible_user_id": other.id, "amount": 200})
    row(db, "deal", "Wrong pipeline Offer%_", {**base, "pipeline_id": other_pipeline.id, "stage_id": other_first.id, "amount": 300})
    row(db, "deal", "Wildcard does not match Offerxx", {**base, "amount": 400})
    archived = row(db, "deal", "Archived Offer%_", {**base, "amount": 500}, archived=True)
    hidden = row(db, "deal", "Hidden", {**base, "amount": 600, "is_unsorted": True})
    inquiry = row(db, "inquiry", "Metadata match", {"pipeline_id": pipeline.id,
        "deal_id": hidden.id, "contact_name": "OFFER%_"})
    db.commit()

    filtered = service.list_entries(db, "deal", query="offer%_", pipeline_id=pipeline.id,
        stage_id=first.id, owner_id=owner.id)
    assert filtered["total"] == 1 and filtered["items"][0]["id"] == target.id
    incoming = service.list_entries(db, "inquiry", query="offer%_", pipeline_id=pipeline.id, owner_id=owner.id)
    assert incoming["total"] == 1 and incoming["items"][0]["id"] == inquiry.id
    result = service.kanban_summary(db, query="offer%_", pipeline_id=pipeline.id, owner_id=owner.id)
    assert column(result, first)["count"] == 2
    assert column(result, first)["amounts"] == {"RUB": 700}
    assert column(result, next_)["amounts"] == {"RUB": 150}
    assert all(item["pipeline_id"] == pipeline.id for item in result["columns"])
    archived_list = service.list_entries(db, "deal", archive="archived", owner_id=owner.id)
    assert archived_list["total"] == 1 and archived_list["items"][0]["id"] == archived.id
    assert column(service.kanban_summary(db, archive="archived", owner_id=owner.id), first)["amounts"] == {"RUB": 500}


def test_owner_filter_preserves_canonical_summary_deduplication(db, make_user):
    owner = make_user(Module.CLIENTS)
    pipeline, first, next_ = board(db)
    regular = row(db, "deal", "Visible", {"pipeline_id": pipeline.id, "stage_id": next_.id,
        "responsible_user_id": owner.id, "amount": 100})
    hidden = row(db, "deal", "Hidden", {"pipeline_id": pipeline.id, "stage_id": first.id,
        "responsible_user_id": owner.id, "amount": 250, "currency": "USD", "is_unsorted": True})
    for linked in (regular, hidden, hidden):
        row(db, "inquiry", "Pending", {"pipeline_id": pipeline.id, "deal_id": linked.id})
    row(db, "inquiry", "Accepted", {"accepted_deal_id": regular.id})
    db.commit()
    result = service.kanban_summary(db, owner_id=owner.id)
    assert column(result, first)["count"] == 1
    assert column(result, first)["amounts"] == {"USD": 250}
    assert column(result, next_)["count"] == 1
    assert column(result, next_)["amounts"] == {"RUB": 100}


@pytest.mark.parametrize("kind", ["deal", "contact", "company", "task"])
def test_non_inquiry_owner_is_its_own_responsible_user(db, make_user, kind):
    owner = make_user(Module.CLIENTS)
    assigned = row(db, kind, "Assigned", {"responsible_user_id": owner.id})
    missing = row(db, kind, "Missing")
    null = row(db, kind, "Null", {"responsible_user_id": None})
    db.commit()
    filtered = service.list_entries(db, kind, owner_id=owner.id)
    assert filtered["total"] == 1 and filtered["items"][0]["id"] == assigned.id
    assert filtered["items"][0]["effective_owner_id"] == owner.id
    unassigned = service.list_entries(db, kind, owner_id=0)
    assert unassigned["total"] == 2
    assert {entry["id"] for entry in unassigned["items"]} == {missing.id, null.id}
    assert all(entry["effective_owner_id"] is None for entry in unassigned["items"])


def test_owner_query_validation_and_read_permissions(db, make_user, api):
    board(db)
    db.commit()
    denied = api(make_user(Module.TASKS, level=AccessLevel.FULL))
    for path in ("/api/crm/entries", "/api/crm/kanban-summary"):
        assert denied.get(path, params={"owner_id": 0}).status_code == 403
    viewer = api(make_user(Module.CLIENTS, level=AccessLevel.VIEW))
    for path in ("/api/crm/entries", "/api/crm/kanban-summary"):
        for value in (-1, "invalid"):
            assert viewer.get(path, params={"owner_id": value}).status_code == 422
        assert viewer.get(path, params={"owner_id": 0}).status_code == 200
        assert viewer.get(path, params={"owner_id": 999999}).status_code == 200
    assert viewer.get("/api/crm/entries", params={"owner_id": 999999}).json() == {"total": 0, "items": []}
    assert all(item["count"] == 0 for item in viewer.get(
        "/api/crm/kanban-summary", params={"owner_id": 999999}).json()["columns"])
