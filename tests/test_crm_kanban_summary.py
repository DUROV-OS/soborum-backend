from copy import deepcopy

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


def test_summary_uses_every_page_and_only_returns_commercial_amounts(db, make_user, api):
    pipeline, first, next_ = board(db)
    for index in range(126):
        row(db, "deal", f"Дом {index}", {"pipeline_id": pipeline.id, "stage_id": first.id,
            "amount": 100.01, "currency": "RUB", "purchase_cost": 999999, "private_note": "PRIVATE COST"})
    row(db, "deal", "Другая стадия", {"pipeline_id": pipeline.id, "stage_id": next_.id, "amount": 240})
    db.commit()
    before = [(entry.id, entry.version, deepcopy(entry.payload)) for entry in db.query(CRMEntity)]
    events = db.query(CRMEvent).count()
    viewer = api(make_user(Module.CLIENTS, level=AccessLevel.VIEW))
    assert viewer.get("/api/crm/entries?kind=deal&limit=100").json()["total"] == 127
    response = viewer.get(f"/api/crm/kanban-summary?pipeline_id={pipeline.id}")
    assert response.status_code == 200
    result = response.json()
    assert column(result, first) == {"pipeline_id": pipeline.id, "stage_id": first.id,
        "count": 126, "amounts": {"RUB": 12601.26}, "unpriced_count": 0}
    assert column(result, next_)["amounts"] == {"RUB": 240}
    assert result["unmapped_count"] == 0
    assert "PRIVATE COST" not in response.text and "purchase_cost" not in response.text
    assert [(entry.id, entry.version, entry.payload) for entry in db.query(CRMEntity)] == before
    assert db.query(CRMEvent).count() == events


def test_summary_counts_linked_inquiry_once_and_preserves_currencies(db):
    pipeline, first, next_ = board(db)
    regular = row(db, "deal", "Visible", {"pipeline_id": pipeline.id, "stage_id": next_.id, "amount": 500})
    hidden = row(db, "deal", "Hidden", {"pipeline_id": pipeline.id, "stage_id": first.id, "is_unsorted": True,
        "amount": 1500.25, "currency": "USD", "cost_price": 1200})
    row(db, "inquiry", "Incoming one", {"pipeline_id": pipeline.id, "deal_id": hidden.id})
    row(db, "inquiry", "Duplicate incoming pointer", {"pipeline_id": pipeline.id, "deal_id": hidden.id})
    row(db, "inquiry", "Stale normal pointer", {"pipeline_id": pipeline.id, "deal_id": regular.id})
    row(db, "inquiry", "Accepted pointer", {"pipeline_id": pipeline.id, "accepted_deal_id": regular.id})
    row(db, "deal", "Rubles", {"pipeline_id": pipeline.id, "stage_id": first.id, "amount": 250, "currency": "RUB"})
    row(db, "deal", "Zero budget", {"pipeline_id": pipeline.id, "stage_id": first.id, "amount": 0})
    row(db, "inquiry", "Website, not priced", {"pipeline_id": None})
    row(db, "inquiry", "Missing deal, not priced", {"pipeline_id": pipeline.id, "deal_id": 999999})
    db.commit()
    result = service.kanban_summary(db)
    assert column(result, first) == {"pipeline_id": pipeline.id, "stage_id": first.id, "count": 5,
        "amounts": {"RUB": 250, "USD": 1500.25}, "unpriced_count": 3}
    assert column(result, next_)["count"] == 1
    assert column(result, next_)["amounts"] == {"RUB": 500}


def test_summary_default_funnel_first_open_stage_and_archive(db):
    other, other_first, _ = board(db, "Другая воронка")
    pipeline, first, _ = board(db)
    row(db, "stage", "Closed earlier", {"pipeline_id": pipeline.id, "sort": -1, "outcome": "lost"})
    row(db, "stage", "Archived earlier", {"pipeline_id": pipeline.id, "sort": -10, "outcome": "open"}, archived=True)
    row(db, "inquiry", "Website default", {})
    row(db, "inquiry", "Archived website", {}, archived=True)
    row(db, "inquiry", "Other pipeline", {"pipeline_id": other.id})
    row(db, "deal", "Active", {"pipeline_id": pipeline.id, "stage_id": first.id, "amount": 100})
    row(db, "deal", "Archived", {"pipeline_id": pipeline.id, "stage_id": first.id, "amount": 200}, archived=True)
    db.commit()
    active = service.kanban_summary(db, pipeline_id=pipeline.id)
    assert column(active, first)["count"] == 2
    assert column(active, first)["amounts"] == {"RUB": 100}
    assert not any(item["pipeline_id"] == other.id for item in active["columns"])
    archived = service.kanban_summary(db, archive="archived", pipeline_id=pipeline.id)
    assert column(archived, first)["count"] == 2
    assert column(archived, first)["amounts"] == {"RUB": 200}
    all_ = service.kanban_summary(db, archive="all")
    assert column(all_, first)["count"] == 4
    assert column(all_, first)["amounts"] == {"RUB": 300}
    assert column(all_, other_first)["count"] == 1


def test_summary_search_matches_register_filters_and_missing_stage_reporting(db):
    pipeline, first, _ = board(db)
    row(db, "deal", "Offer%_", {"pipeline_id": pipeline.id, "stage_id": first.id, "amount": 100})
    row(db, "deal", "Offerxx", {"pipeline_id": pipeline.id, "stage_id": first.id, "amount": 200})
    row(db, "deal", "Metadata match", {"pipeline_id": pipeline.id, "stage_id": first.id, "amount": 300, "custom_fields": ["PHONE123"]})
    hidden = row(db, "deal", "Not the query", {"pipeline_id": pipeline.id, "stage_id": first.id, "amount": 400, "is_unsorted": True})
    row(db, "inquiry", "Other label", {"pipeline_id": pipeline.id, "contact_name": "Match PHONE123", "deal_id": hidden.id})
    row(db, "deal", "Missing stage", {"pipeline_id": pipeline.id, "stage_id": 999999, "amount": 500})
    db.commit()
    literal = service.kanban_summary(db, query="offer%_")
    assert column(literal, first)["count"] == service.list_entries(db, "deal", query="offer%_")["total"] == 1
    assert column(literal, first)["amounts"] == {"RUB": 100}
    metadata = service.kanban_summary(db, query="phone123")
    assert column(metadata, first)["count"] == 2
    assert column(metadata, first)["amounts"] == {"RUB": 700}
    assert service.kanban_summary(db)["unmapped_count"] == 1


def test_summary_permissions_and_query_validation(db, make_user, api):
    board(db)
    db.commit()
    denied = api(make_user(Module.TASKS, level=AccessLevel.FULL))
    assert denied.get("/api/crm/kanban-summary").status_code == 403
    viewer = api(make_user(Module.CLIENTS, level=AccessLevel.VIEW))
    assert viewer.get("/api/crm/kanban-summary").status_code == 200
    assert viewer.get("/api/crm/kanban-summary?archive=deleted").status_code == 422
    assert viewer.get("/api/crm/kanban-summary?query=" + "x" * 301).status_code == 422


def test_summary_unknown_and_invalid_budgets_are_not_fabricated(db):
    pipeline, first, _ = board(db)
    for amount, currency in [(None, "RUB"), ("bad", "RUB"), ("NaN", "RUB"), (-10, "RUB"), (10, "bad")]:
        row(db, "deal", "No usable price", {"pipeline_id": pipeline.id, "stage_id": first.id, "amount": amount, "currency": currency})
    db.commit()
    assert column(service.kanban_summary(db), first) == {"pipeline_id": pipeline.id, "stage_id": first.id,
        "count": 5, "amounts": {}, "unpriced_count": 5}
