"""Демо-сидер производства (app.production.demo_seed) — 0065-d: переводит цикл
демо-клиента с уже загруженными АР/КР в производство и заводит дом с модулем
и просроченной задачей — реальный сигнал для «Главной» (0065-a/b)."""

from app.clients.demo_seed import ensure_demo_clients_seed
from app.core.config import settings
from app.cycle.models import Cycle, CycleStatus
from app.production.demo_seed import ensure_demo_production_seed
from app.production.models import Production, ProductionModule
from app.tasks.models import Task


def test_seed_no_op_without_a_documented_demo_client(db, make_user):
    make_user(admin=True)
    assert ensure_demo_production_seed(db) == 0
    assert db.query(Production).count() == 0


def test_seed_creates_production_module_and_overdue_task(db, make_user):
    make_user(admin=True)
    ensure_demo_clients_seed(db)

    created = ensure_demo_production_seed(db)
    assert created == 1

    production = db.query(Production).one()
    assert db.get(Cycle, production.cycle_id).status == CycleStatus.PRODUCTION
    modules = db.query(ProductionModule).filter(ProductionModule.production_id == production.id).all()
    assert len(modules) == 1
    tasks = db.query(Task).filter(Task.module_id == modules[0].id).all()
    assert len(tasks) == 1
    assert tasks[0].deadline is not None


def test_seed_is_idempotent(db, make_user):
    make_user(admin=True)
    ensure_demo_clients_seed(db)
    ensure_demo_production_seed(db)
    assert ensure_demo_production_seed(db) == 0
    assert db.query(Production).count() == 1


def test_seed_does_not_run_in_prod(db, make_user, monkeypatch):
    make_user(admin=True)
    ensure_demo_clients_seed(db)
    monkeypatch.setattr(settings, "app_env", "prod")
    assert ensure_demo_production_seed(db) == 0
    assert db.query(Production).count() == 0
