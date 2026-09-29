"""Task 0084-h: число «Открытых задач» на Пульсе равно числу задач на доске
(`GET /api/tasks?scope=…&status=open`) при тех же правах пользователя."""

import importlib.util
from pathlib import Path

from app.common.module_access import Module
from app.dashboard.overview import generate_today
from app.tasks.models import Task, TaskLinkType, TaskStatus


def _seed(db, worker, other):
    mine = Task(title="mine", status=TaskStatus.IN_PROGRESS)
    mine.assignees = [worker]
    reviewing = Task(title="reviewing", status=TaskStatus.IN_REVIEW)
    reviewing.assignees = [other]
    reviewing.reviewers = [worker]
    others = Task(title="others", status=TaskStatus.READY)
    others.assignees = [other]
    not_ready = Task(title="not ready, no assignee", status=TaskStatus.NOT_READY)
    free = Task(title="free", status=TaskStatus.READY)
    free_warehouse = Task(
        title="free warehouse", status=TaskStatus.READY,
        link_type=TaskLinkType.WAREHOUSE_REQUEST, link_id=1,
    )
    done = Task(title="done", status=TaskStatus.DONE)
    done.assignees = [worker]
    db.add_all([mine, reviewing, others, not_ready, free, free_warehouse, done])
    db.commit()


def _open_widget(db, user):
    out = generate_today(db, user)
    return next(w for w in out.widgets if w.section == "tasks" and w.title.startswith("Открытых задач"))


def test_admin_counter_equals_all_board_open(db, make_user, api):
    admin = make_user(admin=True)
    worker = make_user(Module.TASKS)
    other = make_user(Module.TASKS)
    _seed(db, worker, other)

    widget = _open_widget(db, admin)
    board = api(admin).get("/api/tasks/?scope=all&status=open")

    assert board.status_code == 200
    assert widget.title == "Открытых задач (все)"
    assert widget.href == "/tasks?scope=all&status=open"
    assert int(widget.value) == len(board.json()) == 6
    assert all(t["status"] != "done" for t in board.json())


def test_worker_counter_equals_mine_board_open(db, make_user, api):
    worker = make_user(Module.TASKS)
    other = make_user(Module.TASKS)
    _seed(db, worker, other)

    widget = _open_widget(db, worker)
    board = api(worker).get("/api/tasks/?scope=mine&status=open")

    assert board.status_code == 200
    assert widget.title == "Открытых задач (мои)"
    assert widget.href == "/tasks?scope=mine&status=open"
    # Свои (исполнитель/проверяющий) и свободная без раздела; чужая, not_ready
    # без исполнителя, свободная складская без прав склада и done — нет.
    assert {t["title"] for t in board.json()} == {"mine", "reviewing", "free"}
    assert int(widget.value) == len(board.json())


def test_task_for_another_employee_changes_only_admin_counter(db, make_user, api):
    admin = make_user(admin=True)
    worker = make_user(Module.TASKS)
    other = make_user(Module.TASKS)
    _seed(db, worker, other)
    admin_before = int(_open_widget(db, admin).value)
    worker_before = int(_open_widget(db, worker).value)

    created = api(admin).post("/api/tasks/", json={"title": "new for other", "assignee_ids": [other.id]})
    assert created.status_code == 201

    assert int(_open_widget(db, admin).value) == admin_before + 1
    assert int(_open_widget(db, worker).value) == worker_before


def test_status_open_excludes_done_and_plain_status_still_filters(db, make_user, api):
    worker = make_user(Module.TASKS)
    other = make_user(Module.TASKS)
    _seed(db, worker, other)
    client = api(worker)

    open_titles = {t["title"] for t in client.get("/api/tasks/?status=open").json()}
    done_titles = {t["title"] for t in client.get("/api/tasks/?status=done").json()}
    everything = {t["title"] for t in client.get("/api/tasks/").json()}

    assert "done" not in open_titles
    assert done_titles == {"done"}
    assert everything == open_titles | done_titles
    assert client.get("/api/tasks/?status=bogus").status_code == 422


def test_diagnose_script_reports_same_set_as_board(db, make_user, api):
    worker = make_user(Module.TASKS)
    other = make_user(Module.TASKS)
    _seed(db, worker, other)
    path = Path(__file__).resolve().parent.parent / "scripts" / "diagnose_task_counts.py"
    spec = importlib.util.spec_from_file_location("diagnose_task_counts", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    lines = module.diagnose(db, worker)

    board_ids = sorted(t["id"] for t in api(worker).get("/api/tasks/?status=open").json())
    idx = lines.index("Пульс сейчас (open_tasks_query, scope=mine): 3")
    assert lines[idx + 1].strip() == ", ".join(str(i) for i in board_ids)
    assert any("not_ready — свободной не считается" in line for line in lines)
    assert any("нет прав раздела warehouse" in line for line in lines)
