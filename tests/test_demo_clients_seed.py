"""Демо-сидер мокнутых клиентов (app.clients.demo_seed) — 0025.

Идемпотентность, prod-guard, и что клиенты/циклы создаются через обычный
create_client (не голым INSERT), так что «Актуальное» на «Пульсе» находит
активность по ним.
"""

from app.clients.demo_seed import ensure_demo_clients_seed
from app.clients.models import Client, ClientNote
from app.core.config import settings
from app.cycle.models import Cycle
from app.dashboard.aktualnoe import collect_cycle_activity
from app.users.models import User, UserRole


def test_seed_creates_clients_with_cycles_and_notes(db, make_user):
    make_user(admin=True)

    created = ensure_demo_clients_seed(db)
    assert created == 3

    clients = db.query(Client).all()
    assert len(clients) == 3
    assert db.query(Cycle).count() == 3
    assert db.query(ClientNote).count() >= 1


def test_seed_populates_aktualnoe_activity(db, make_user):
    make_user(admin=True)
    ensure_demo_clients_seed(db)

    activity = collect_cycle_activity(db)
    assert len(activity) == 3


def test_seed_is_idempotent(db, make_user):
    make_user(admin=True)
    first = ensure_demo_clients_seed(db)
    assert first > 0
    before = db.query(Client).count()

    assert ensure_demo_clients_seed(db) == 0
    assert db.query(Client).count() == before


def test_seed_skips_if_clients_already_exist(db, make_user):
    make_user(admin=True)
    from app.clients import service as client_service
    from app.clients.schemas import ClientCreate

    client_service.create_client(
        db, ClientCreate(full_name="Свой клиент", phone="+70000000000", email="own@example.com")
    )
    db.commit()

    assert ensure_demo_clients_seed(db) == 0
    assert db.query(Client).count() == 1


def test_seed_does_not_run_in_prod(db, monkeypatch):
    monkeypatch.setattr(settings, "app_env", "prod")
    assert ensure_demo_clients_seed(db) == 0
    assert db.query(Client).count() == 0
