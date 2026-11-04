from app.common.module_access import AccessLevel, Module
from app.crm import service
from app.crm.models import CRMEntity, CRMTaskLink
from app.crm.schemas import Patch
from app.tasks.models import Task, TaskPriority


def add(db, name, priority=None, *, owner_id=None, archived=False):
    crm = CRMEntity(kind="task", name=name, payload={"responsible_user_id": owner_id}, archived=archived)
    db.add(crm)
    db.flush()
    if priority is not None:
        task = Task(title=name, priority=priority)
        db.add(task)
        db.flush()
        db.add(CRMTaskLink(entity_id=crm.id, task_id=task.id))
    return crm


def test_crm_priority_comes_only_from_canonical_task_and_filters_before_pagination(db, make_user, api):
    owner = make_user(Module.CLIENTS)
    other = make_user(Module.CLIENTS)
    ids = {add(db, "Match high", TaskPriority.HIGH, owner_id=owner.id).id for _ in range(125)}
    add(db, "Match high other owner", TaskPriority.HIGH, owner_id=other.id)
    add(db, "Different text", TaskPriority.HIGH, owner_id=owner.id)
    add(db, "Match archived", TaskPriority.HIGH, owner_id=owner.id, archived=True)
    medium = add(db, "Match medium", TaskPriority.MEDIUM, owner_id=owner.id)
    low = add(db, "Match low", TaskPriority.LOW, owner_id=owner.id)
    unlinked = add(db, "Match no link", owner_id=owner.id)
    # An untrusted/old extra payload value is not the shared task's priority.
    unlinked.payload = {**unlinked.payload, "priority": "high"}
    db.commit()
    viewer = api(make_user(Module.CLIENTS, level=AccessLevel.VIEW))
    params = {"kind": "task", "priority": "high", "owner_id": owner.id, "query": "match", "limit": 100}
    pages = [viewer.get("/api/crm/entries", params={**params, "offset": offset}).json() for offset in (0, 100)]
    assert [page["total"] for page in pages] == [125, 125]
    assert {entry["id"] for page in pages for entry in page["items"]} == ids
    assert all(entry["priority"] == "high" for page in pages for entry in page["items"])
    assert service.out(medium)["priority"] == "medium"
    assert service.out(low)["priority"] == "low"
    assert service.out(unlinked)["priority"] is None
    assert viewer.get(f"/api/crm/entries/{medium.id}").json()["priority"] == "medium"
    assert service.list_entries(db, "task", priority="high", archive="archived")["total"] == 1
    assert service.list_entries(db, "task")["total"] == 130


def test_crm_edits_preserve_existing_task_priority(db, make_user):
    user = make_user(Module.CLIENTS)
    other = make_user(Module.CLIENTS)
    crm = add(db, "High task", TaskPriority.HIGH, owner_id=user.id)
    db.commit()
    changed = service.patch(db, crm.id, Patch(version=crm.version, name="Renamed",
        data={"description": "More context", "responsible_user_id": other.id, "completed": True}), user)
    assert changed["priority"] == "high"
    assert "priority" not in changed["data"]
    link = db.get(CRMTaskLink, crm.id)
    assert db.get(Task, link.task_id).priority == TaskPriority.HIGH


def test_crm_priority_query_validation_permissions_and_kind(db, make_user, api):
    client = api(make_user(Module.CLIENTS, level=AccessLevel.VIEW))
    assert client.get("/api/crm/entries?kind=task&priority=urgent").status_code == 422
    assert client.get("/api/crm/entries?kind=deal&priority=high").status_code == 422
    assert client.get("/api/crm/entries?kind=task&priority=medium").status_code == 200
    assert api(make_user(Module.TASKS)).get("/api/crm/entries?kind=task&priority=high").status_code == 403
