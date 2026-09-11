"""Демо-сидер мокнутых сотрудников и задач (app.tasks.demo_seed) — 0024.

Идемпотентность, prod-guard, и что задачи/проводки действительно проходят
через обычную статусную машину (не голый INSERT в разные статусы).
"""

from app.accounting.models import MoneyMovement, MoneyMovementStatus, MoneySubkind
from app.core.config import settings
from app.tasks.demo_seed import ensure_demo_workforce_seed
from app.tasks.models import Task, TaskStatus
from app.users.models import User, UserRole


def test_seed_creates_workers_with_tasks_in_mixed_statuses(db):
    created = ensure_demo_workforce_seed(db)
    assert created == 5

    workers = db.query(User).filter(User.role == UserRole.WORKER).all()
    assert len(workers) == 5
    assert all(w.email.startswith("demo.") for w in workers)

    tasks = db.query(Task).all()
    assert tasks
    statuses = {t.status for t in tasks}
    assert TaskStatus.DONE in statuses
    assert statuses & {TaskStatus.IN_PROGRESS, TaskStatus.READY}

    done_with_reviewer = next(t for t in tasks if t.status is TaskStatus.DONE and t.reviewers)
    assert done_with_reviewer.reviewers[0].id != done_with_reviewer.assignees[0].id


def test_seed_creates_salary_movements_for_some_employees(db):
    ensure_demo_workforce_seed(db)

    salaries = db.query(MoneyMovement).filter(MoneyMovement.subkind == MoneySubkind.SALARY_PAYOUT).all()
    assert len(salaries) == 2
    statuses = {m.status for m in salaries}
    assert statuses == {MoneyMovementStatus.APPROVED, MoneyMovementStatus.POSTED}
    posted = next(m for m in salaries if m.status is MoneyMovementStatus.POSTED)
    assert posted.posted_at is not None


def test_seed_is_idempotent(db):
    first = ensure_demo_workforce_seed(db)
    assert first > 0
    before = db.query(User).count()

    assert ensure_demo_workforce_seed(db) == 0
    assert db.query(User).count() == before


def test_seed_skips_if_workers_already_exist(db, make_user):
    make_user()  # обычный WORKER, не демо
    assert ensure_demo_workforce_seed(db) == 0
    assert db.query(Task).count() == 0


def test_seed_does_not_run_in_prod(db, monkeypatch):
    monkeypatch.setattr(settings, "app_env", "prod")
    assert ensure_demo_workforce_seed(db) == 0
    assert db.query(User).filter(User.role == UserRole.WORKER).count() == 0
