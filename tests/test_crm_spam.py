from copy import deepcopy
from datetime import datetime, timezone

from app.common.module_access import AccessLevel, Module
from app.crm import service
from app.crm.models import CRMEntity, CRMEvent


CREATED = datetime(2026, 11, 4, 9, tzinfo=timezone.utc)


def row(db, kind, data=None, *, name="Match", archived=False, created=CREATED):
    result = CRMEntity(kind=kind, name=name, payload=data or {}, archived=archived, created_at=created)
    db.add(result)
    db.flush()
    return result


def board(db):
    pipeline = row(db, "pipeline", name="Допродажная воронка")
    stage = row(db, "stage", {"pipeline_id": pipeline.id, "sort": 1, "outcome": "open"})
    return pipeline, stage


def column(result, stage):
    return next(item for item in result["columns"] if item["stage_id"] == stage.id)


def test_explicit_spam_and_undo_are_versioned_preserve_source_and_update_all_views(db, make_user, api):
    user = make_user(Module.CLIENTS)
    pipeline, stage = board(db)
    base = {"pipeline_id": pipeline.id, "stage_id": stage.id, "responsible_user_id": user.id}
    hidden = row(db, "deal", {**base, "is_unsorted": True, "amount": 100})
    inquiry = row(db, "inquiry", {"pipeline_id": pipeline.id, "deal_id": hidden.id, "source_name": "Website", "reason": "Keep this"})
    row(db, "deal", {**base, "amount": 400})
    db.commit()
    linked_before = deepcopy(hidden.payload)
    count_before = db.query(CRMEntity).count()
    original_date = service.out(inquiry)["source_created_at"]
    client = api(user)
    response = client.patch(f"/api/crm/entries/{inquiry.id}", json={"version": 1, "data": {"is_spam": True}})
    assert response.status_code == 200, response.text
    marked = response.json()
    assert marked["version"] == 2 and marked["data"]["is_spam"] is True
    assert marked["archived"] is False and marked["data"]["accepted_deal_id"] is None
    assert marked["data"]["deal_id"] == hidden.id and marked["data"]["reason"] == "Keep this"
    assert marked["source_created_at"] == original_date
    assert client.get("/api/crm/entries?kind=inquiry").json()["total"] == 0
    assert client.get("/api/crm/entries?kind=inquiry&spam=spam").json()["items"][0]["id"] == inquiry.id
    assert client.get("/api/crm/entries?kind=inquiry&spam=all").json()["total"] == 1
    assert client.get("/api/crm/entries?kind=deal&spam=spam").json()["total"] == 0
    assert client.get("/api/crm/catalog").json()["counts"]["inquiry"] == 0
    for mode, count, amount in [("normal", 1, 400), ("spam", 1, 100), ("all", 2, 500)]:
        summary = client.get("/api/crm/kanban-summary", params={"spam": mode}).json()
        assert column(summary, stage)["count"] == count
        assert column(summary, stage)["amounts"] == {"RUB": amount}
    assert client.patch(f"/api/crm/entries/{inquiry.id}", json={"version": 1, "data": {"is_spam": False}}).status_code == 409
    response = client.patch(f"/api/crm/entries/{inquiry.id}", json={"version": 2, "data": {"is_spam": False}})
    assert response.status_code == 200 and response.json()["version"] == 3
    assert response.json()["archived"] is False
    assert client.get("/api/crm/entries?kind=inquiry").json()["total"] == 1
    assert client.get("/api/crm/entries?kind=inquiry&spam=spam").json()["total"] == 0
    assert client.get("/api/crm/catalog").json()["counts"]["inquiry"] == 1
    assert column(service.kanban_summary(db), stage)["amounts"] == {"RUB": 500}
    events = db.query(CRMEvent).filter_by(entity_id=inquiry.id).order_by(CRMEvent.id).all()
    assert [event.action for event in events] == ["updated", "updated"]
    assert [event.details["after"]["payload"]["is_spam"] for event in events] == [True, False]
    assert all(event.actor_id == user.id for event in events)
    assert hidden.payload == linked_before and db.query(CRMEntity).count() == count_before


def test_spam_filter_precedes_paging_and_agrees_with_owner_dates_query_and_budgets(db, make_user, api):
    owner = make_user(Module.CLIENTS, level=AccessLevel.VIEW)
    other = make_user(Module.CLIENTS)
    pipeline, stage = board(db)
    base = {"pipeline_id": pipeline.id, "stage_id": stage.id, "responsible_user_id": owner.id, "amount": 20, "is_unsorted": True}
    expected = set()
    for _ in range(125):
        hidden = row(db, "deal", base)
        expected.add(row(db, "inquiry", {"pipeline_id": pipeline.id, "deal_id": hidden.id, "is_spam": True}).id)
    row(db, "inquiry", {"pipeline_id": pipeline.id})
    row(db, "inquiry", {"pipeline_id": pipeline.id, "is_spam": False})
    for changes, extra in [({"responsible_user_id": other.id}, {}), ({}, {"created": datetime(2026, 11, 3, 9, tzinfo=timezone.utc)}),
                           ({}, {"name": "Different"}), ({}, {"archived": True})]:
        hidden = row(db, "deal", {**base, **changes})
        row(db, "inquiry", {"pipeline_id": pipeline.id, "deal_id": hidden.id, "is_spam": True}, **extra)
    db.commit()
    client = api(owner)
    params = {"spam": "spam", "owner_id": owner.id, "date_from": "2026-11-04", "date_to": "2026-11-04", "query": "match", "pipeline_id": pipeline.id}
    pages = [client.get("/api/crm/entries", params={"kind": "inquiry", **params, "limit": 100, "offset": offset}).json() for offset in (0, 100)]
    assert [page["total"] for page in pages] == [125, 125]
    assert {entry["id"] for page in pages for entry in page["items"]} == expected
    assert all(entry["data"]["is_spam"] is True for page in pages for entry in page["items"])
    assert column(client.get("/api/crm/kanban-summary", params=params).json(), stage) == {
        "pipeline_id": pipeline.id, "stage_id": stage.id, "count": 125, "amounts": {"RUB": 2500}, "unpriced_count": 0}
    assert service.list_entries(db, "inquiry")["total"] == 2
    assert service.list_entries(db, "inquiry", spam="all")["total"] == 130


def test_spam_cannot_mark_accepted_archived_or_visible_deal_and_requires_undo_before_accept(db, make_user, api):
    editor = make_user(Module.CLIENTS)
    pipeline, stage = board(db)
    base = {"pipeline_id": pipeline.id, "stage_id": stage.id}
    visible = row(db, "deal", {**base, "amount": 400})
    accepted = row(db, "inquiry", {"pipeline_id": pipeline.id, "accepted_deal_id": visible.id})
    archived = row(db, "inquiry", {"pipeline_id": pipeline.id}, archived=True)
    stale_link = row(db, "inquiry", {"pipeline_id": pipeline.id, "deal_id": visible.id})
    spam = row(db, "inquiry", {"pipeline_id": pipeline.id, "is_spam": True})
    db.commit()
    client = api(editor)
    for entry in (accepted, archived, stale_link, visible):
        response = client.patch(f"/api/crm/entries/{entry.id}", json={"version": entry.version, "data": {"is_spam": True}})
        assert response.status_code == 422
    assert client.post(f"/api/crm/entries/{spam.id}/accept", json={"name": "Must undo first", "data": base}).status_code == 422
    assert db.query(CRMEvent).count() == 0
    assert visible.payload["amount"] == 400 and not visible.archived
    assert service.list_entries(db, "deal")["items"][0]["id"] == visible.id


def test_spam_filters_validate_and_preserve_permissions(db, make_user, api):
    inquiry = row(db, "inquiry")
    db.commit()
    viewer = api(make_user(Module.CLIENTS, level=AccessLevel.VIEW))
    assert viewer.patch(f"/api/crm/entries/{inquiry.id}", json={"version": 1, "data": {"is_spam": True}}).status_code == 403
    for endpoint in ("/api/crm/entries", "/api/crm/kanban-summary"):
        assert viewer.get(endpoint, params={"spam": "invalid"}).status_code == 422
        assert viewer.get(endpoint, params={"spam": "spam"}).status_code == 200
    denied = api(make_user(Module.TASKS))
    assert denied.get("/api/crm/entries?spam=all").status_code == 403
    assert denied.get("/api/crm/kanban-summary?spam=spam").status_code == 403


def test_attention_suppresses_only_pending_spam_signals_and_undo_restores_shared_tasks(db, make_user, api):
    from app.crm.guard import attention
    from app.crm.models import CRMTaskLink
    from app.tasks.models import Task

    user = make_user(Module.CLIENTS)
    pipeline, stage = board(db)
    hidden = row(db, "deal", {"pipeline_id": pipeline.id, "stage_id": stage.id, "is_unsorted": True})
    incoming = row(db, "inquiry", {"pipeline_id": pipeline.id, "deal_id": hidden.id})
    visible = row(db, "deal", {"pipeline_id": pipeline.id, "stage_id": stage.id, "responsible_user_id": user.id})
    # A legacy invalid flag on an accepted row must not hide its actual deal.
    accepted = row(db, "inquiry", {"accepted_deal_id": visible.id, "is_spam": True})
    crm_tasks, shared_tasks = [], []
    for target in (incoming, hidden, visible, accepted):
        crm_task = row(db, "task", {"entity_id": target.id, "due_at": "2026-11-03T00:00:00+00:00", "responsible_user_id": user.id})
        task = Task(title="Shared task")
        db.add(task)
        db.flush()
        db.add(CRMTaskLink(entity_id=crm_task.id, task_id=task.id))
        crm_tasks.append(crm_task)
        shared_tasks.append(task)
    db.commit()
    state_before = [(task.id, task.status, task.priority) for task in shared_tasks]
    client = api(user)
    initial = attention(db, now=CREATED)
    assert {task.id for task in crm_tasks}.issubset({signal["entry_id"] for signal in initial["items"]})
    assert client.patch(f"/api/crm/entries/{incoming.id}", json={"version": 1, "data": {"is_spam": True}}).status_code == 200
    db.expire_all()
    flagged = attention(db, now=CREATED)
    ids = {signal["entry_id"] for signal in flagged["items"]}
    assert incoming.id not in ids and crm_tasks[0].id not in ids and crm_tasks[1].id not in ids
    assert {accepted.id, crm_tasks[2].id, crm_tasks[3].id}.issubset(ids)
    assert service.list_entries(db, "deal")["items"][0]["id"] == visible.id
    assert [(task.id, task.status, task.priority) for task in shared_tasks] == state_before
    assert all(not task.archived and not task.payload.get("completed") for task in crm_tasks)
    assert client.patch(f"/api/crm/entries/{incoming.id}", json={"version": 2, "data": {"is_spam": False}}).status_code == 200
    restored = attention(db, now=CREATED)
    assert {signal["entry_id"] for signal in restored["items"]} == {signal["entry_id"] for signal in initial["items"]}
