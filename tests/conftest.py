import os

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["JWT_SECRET"] = "isolated-regression-test-secret-never-use-in-production"
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["STORAGE_DIR"] = "/tmp/durov-os-tests"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

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
        yield session
    engine.dispose()


@pytest.fixture
def make_user(db):
    def create(*modules, admin=False):
        user = User(email=f"user{db.query(User).count()}@example.com", full_name="Тестовый сотрудник",
                    hashed_password="not-a-login-password", is_active=True,
                    role=UserRole.ADMIN if admin else UserRole.WORKER)
        user.module_access = [UserModuleAccess(module=module) for module in modules]
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
