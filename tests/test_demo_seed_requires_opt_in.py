"""2026-09-17 incident: demo/mock data reached a real deployment because that
deployment's APP_ENV wasn't recognized as prod, and every demo_seed module
only checked `is_prod`. Now every demo seed requires an EXPLICIT
`ENABLE_DEMO_SEED=1` on top of a non-prod APP_ENV (Settings.should_seed_demo_data)
— a misconfigured or unset APP_ENV alone must never be enough."""

from app.accounting.seed import ensure_accounting_seed
from app.ai.demo_seed import ensure_agent_activity_seed, ensure_growth_proposals_seed
from app.clients.demo_seed import ensure_demo_clients_seed
from app.clients.models import Client
from app.core.config import settings
from app.tasks.demo_seed import ensure_demo_workforce_seed
from app.users.models import User


def test_should_seed_demo_data_requires_both_flags(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "dev")
    monkeypatch.setattr(settings, "enable_demo_seed", False)
    assert settings.should_seed_demo_data is False

    monkeypatch.setattr(settings, "enable_demo_seed", True)
    assert settings.should_seed_demo_data is True

    monkeypatch.setattr(settings, "app_env", "prod")
    assert settings.should_seed_demo_data is False


def test_demo_seeds_are_no_op_without_explicit_opt_in_even_in_dev(db, monkeypatch):
    # APP_ENV=dev (is_prod=False) alone must not be enough — this is exactly
    # the misconfiguration that let demo data reach a real deployment.
    monkeypatch.setattr(settings, "app_env", "dev")
    monkeypatch.setattr(settings, "enable_demo_seed", False)

    assert ensure_demo_clients_seed(db) == 0
    assert db.query(Client).count() == 0

    assert ensure_demo_workforce_seed(db) == 0
    assert db.query(User).count() == 0

    assert ensure_accounting_seed(db) == 0
    assert ensure_agent_activity_seed(db) == 0
    assert ensure_growth_proposals_seed(db) == 0
