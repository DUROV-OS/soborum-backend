"""Task 0021: GET /api/tasks/?scope=mine|claimable|all and POST /claim."""

from app.common.module_access import Module
from app.tasks.models import Task, TaskLinkType, TaskStatus


def _seed_tasks(db, worker, other):
    assigned_to_me = Task(title="assigned to me", status=TaskStatus.IN_PROGRESS)
    assigned_to_me.assignees = [worker]

    assigned_to_other = Task(title="assigned to someone else", status=TaskStatus.READY)
    assigned_to_other.assignees = [other]

    free_unbound = Task(title="free, no section", status=TaskStatus.READY)

    free_warehouse = Task(
        title="free, warehouse", status=TaskStatus.READY,
        link_type=TaskLinkType.WAREHOUSE_REQUEST, link_id=1,
    )

    free_production = Task(title="free, production module", status=TaskStatus.READY, module_id=999)

    free_clients = Task(
        title="free, clients", status=TaskStatus.READY,
        link_type=TaskLinkType.CLIENT_STAGE, link_id=1,
    )

    db.add_all([
        assigned_to_me, assigned_to_other, free_unbound,
        free_warehouse, free_production, free_clients,
    ])
    db.commit()
    return {
        "assigned_to_me": assigned_to_me,
        "assigned_to_other": assigned_to_other,
        "free_unbound": free_unbound,
        "free_warehouse": free_warehouse,
        "free_production": free_production,
        "free_clients": free_clients,
    }


def test_scope_mine_is_default_and_combines_assigned_with_claimable(db, make_user, api):
    worker = make_user(Module.TASKS, Module.WAREHOUSE)
    other = make_user(Module.TASKS)
    tasks = _seed_tasks(db, worker, other)

    client = api(worker)
    resp = client.get("/api/tasks/")

    assert resp.status_code == 200
    titles = {t["title"] for t in resp.json()}
    assert titles == {
        tasks["assigned_to_me"].title,
        tasks["free_unbound"].title,
        tasks["free_warehouse"].title,
    }


def test_scope_claimable_excludes_already_assigned_tasks(db, make_user, api):
    worker = make_user(Module.TASKS, Module.WAREHOUSE)
    other = make_user(Module.TASKS)
    tasks = _seed_tasks(db, worker, other)

    client = api(worker)
    resp = client.get("/api/tasks/?scope=claimable")

    assert resp.status_code == 200
    titles = {t["title"] for t in resp.json()}
    assert titles == {tasks["free_unbound"].title, tasks["free_warehouse"].title}


def test_scope_all_forbidden_without_grant(db, make_user, api):
    worker = make_user(Module.TASKS)
    _seed_tasks(db, worker, worker)

    client = api(worker)
    resp = client.get("/api/tasks/?scope=all")

    assert resp.status_code == 403


def test_scope_all_allowed_for_admin_and_granted_worker(db, make_user, api):
    admin = make_user(admin=True)
    granted_worker = make_user(Module.TASKS, Module.TASKS_ALL)
    _seed_tasks(db, admin, granted_worker)

    for user in (admin, granted_worker):
        resp = api(user).get("/api/tasks/?scope=all")
        assert resp.status_code == 200
        assert len(resp.json()) == 6


def test_claim_assigns_current_user_to_free_task(db, make_user, api):
    worker = make_user(Module.TASKS, Module.WAREHOUSE)
    tasks = _seed_tasks(db, worker, worker)

    resp = api(worker).post(f"/api/tasks/{tasks['free_warehouse'].id}/claim")

    assert resp.status_code == 200
    body = resp.json()
    assert [a["id"] for a in body["assignees"]] == [worker.id]


def test_claim_rejects_task_with_existing_assignee(db, make_user, api):
    worker = make_user(Module.TASKS)
    other = make_user(Module.TASKS)
    tasks = _seed_tasks(db, worker, other)

    resp = api(worker).post(f"/api/tasks/{tasks['assigned_to_other'].id}/claim")

    assert resp.status_code == 400


def test_claim_rejects_task_in_section_without_access(db, make_user, api):
    worker = make_user(Module.TASKS)
    tasks = _seed_tasks(db, worker, worker)

    resp = api(worker).post(f"/api/tasks/{tasks['free_production'].id}/claim")

    assert resp.status_code == 403
