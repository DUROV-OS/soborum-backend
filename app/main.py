from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app.ai import mcp_auth
from app.ai.router import app as ai_app
from app.board.router import app as board_app
from app.board.seed import ensure_seed
from app.clients.router import app as clients_app
from app.common.files import router as files_router
from app.core.config import settings
from app.cycle.router import app as cycle_app
from app.dashboard.router import app as dashboard_app
from app.db import import_all_models  # noqa: F401  (registers all models with Base.metadata)
from app.db.base import Base
from app.db.session import engine, get_db, SessionLocal
from app.installation.router import app as installation_app
from app.marketing.router import app as marketing_app
from app.production.router import app as production_app
from app.tasks.router import app as tasks_app
from app.users.router import app as auth_app
from app.users.service import bootstrap_admin
from app.warehouse.router import app as warehouse_app

app = FastAPI(title="Soborbum")

# Applied at the root app, so it also covers requests routed into the mounted
# per-section sub-apps below (CORSMiddleware wraps the whole ASGI stack,
# including Mount dispatch) - the frontend only ever talks to this one origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup() -> None:
    # Alembic owns schema management in normal operation (see entrypoint.sh);
    # create_all is a harmless no-op once migrations have run, and keeps a
    # fresh dev DB usable even before the first `alembic upgrade head`.
    if not inspect(engine).get_table_names():
        Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        bootstrap_admin(db)
        # Idempotent: only actually creates anything the first time it runs
        # after a deploy, no-op on every restart after that (see the module).
        ensure_seed(db)
    finally:
        db.close()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/callback")
def mcp_oauth_callback(code: str, state: str, db: Session = Depends(get_db)):
    """Redirect target for the one-time MCP authorization (see
    GET /api/ai/mcp/authorize). Lives at exactly this path on the root app,
    not under /api/ai, because it has to match the redirect_uri registered
    with the MCP provider byte-for-byte."""
    mcp_auth.exchange_code_for_tokens(db, code, state)
    return {"status": "ok", "detail": "MCP авторизован, можно закрыть эту вкладку."}


app.include_router(files_router)

app.mount("/api/auth", auth_app)
app.mount("/api/clients", clients_app)
app.mount("/api/production", production_app)
app.mount("/api/installation", installation_app)
app.mount("/api/cycles", cycle_app)
app.mount("/api/warehouse", warehouse_app)
app.mount("/api/marketing", marketing_app)
app.mount("/api/tasks", tasks_app)
app.mount("/api/ai", ai_app)
app.mount("/api/dashboard", dashboard_app)
app.mount("/api/board", board_app)
