import os

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["APP_ENV"] = "dev"
os.environ["ENABLE_DEMO_SEED"] = "true"
os.environ["JWT_SECRET"] = "isolated-regression-test-secret-never-use-in-production"
os.environ["ADMIN_PASSWORD"] = ""
os.environ["CORS_ALLOWED_ORIGINS"] = "http://localhost:5173"
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["STORAGE_DIR"] = "/tmp/durov-os-tests"
os.environ["AGENT_SHIFT_AUTORUN"] = "false"
os.environ["MCP_SERVER_URL"] = ""
os.environ["MCP_OAUTH_CLIENT_ID"] = ""
os.environ["MCP_OAUTH_CLIENT_SECRET"] = ""
os.environ["MOYSKLAD_MCP_URL"] = ""
os.environ["MOYSKLAD_MCP_CLIENT_ID"] = ""
os.environ["MOYSKLAD_MCP_CLIENT_SECRET"] = ""
os.environ["DASHBOARD_MCP_URL"] = ""
os.environ["DASHBOARD_MCP_CLIENT_ID"] = ""
os.environ["DASHBOARD_MCP_CLIENT_SECRET"] = ""
os.environ["MOYSKLAD_TOKEN"] = ""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.accounting.seed import ensure_organizations_seed
from app.common.module_access import AccessLevel
from app.db import import_all_models  # noqa: F401
from app.db.base import Base
from app.db.session import get_db
from app.core.deps import get_current_user
from app.main import app
from app.users.models import User, UserModuleAccess, UserRole


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        # Организации и их счета — не демо-данные, а обязательный справочник
        # (0081-a): без счёта проводку создать нельзя, в проде их заводит
        # миграция/стартовый сид. Тестовая база должна быть в том же состоянии.
        ensure_organizations_seed(session)
        yield session
    engine.dispose()


@pytest.fixture
def make_user(db):
    def create(*modules, admin=False, level=AccessLevel.EDIT):
        """`level` — уровень гранта на каждый из `modules` (задача 0052).
        По умолчанию `EDIT`: раньше грант означал безусловный доступ ко всем
        операциям раздела, `EDIT` — ближайший аналог для разделов, ещё не
        переведённых на require_view/edit/full (has_access не смотрит на
        уровень). Разделу «Клиенты» (пилот) — указывать level явно."""
        user = User(email=f"user{db.query(User).count()}@example.com", full_name="Тестовый сотрудник",
                    hashed_password="not-a-login-password", is_active=True,
                    role=UserRole.ADMIN if admin else UserRole.WORKER)
        user.module_access = [UserModuleAccess(module=module, level=level) for module in modules]
        db.add(user)
        db.commit()
        return user
    return create


@pytest.fixture
def api(db):
    apps = [app] + [r.app for r in app.routes if hasattr(r, "app") and hasattr(r.app, "dependency_overrides")]
    def authenticated(user):
        for subapp in apps:
            subapp.dependency_overrides[get_db] = lambda: db
            subapp.dependency_overrides[get_current_user] = lambda: user
        return TestClient(app)
    yield authenticated
    for subapp in apps:
        subapp.dependency_overrides.clear()


@pytest.fixture
def default_account(db):
    """Счёт по умолчанию первой организации (0081-a) — на него тесты заводят
    проводки там, где сам счёт не является предметом проверки."""
    from app.accounting.models import BankAccount

    return (
        db.query(BankAccount)
        .filter(BankAccount.is_default.is_(True))
        .order_by(BankAccount.id)
        .first()
    )
