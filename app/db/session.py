import json

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings

# default=str so JSON columns (e.g. ai_messages.tool_resolutions, which embeds
# raw tool-handler output like get_modules'/list_materials' created_at fields)
# don't blow up on datetimes - stdlib json.dumps has no idea how to serialize them.
engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    json_serializer=lambda obj: json.dumps(obj, default=str, ensure_ascii=False),
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


@event.listens_for(Session, "after_begin")
def restore_integrity_actor(session, transaction, connection):
    from app.integrity.context import apply_actor
    # A request may commit several times. SET LOCAL expires on every commit,
    # so restore the authenticated context for each subsequent transaction.
    apply_actor(connection, session.info.get("integrity_actor", {"kind": "system_or_unattributed"}))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
