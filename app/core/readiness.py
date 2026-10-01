"""Readiness probe for deploys (0084-a).

`/health` only says the process is alive. `/ready` says the process can do its
job: the database answers and its schema is at the revision the code expects.
Deploy waits for `/ready` and turns red otherwise, so a failed
`alembic upgrade head` (e.g. two heads, PR #79) no longer hides behind a green CI.

The response carries only a short reason code — never the connection string
or driver error text.
"""

from functools import lru_cache
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.orm import Session

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"

DB_UNREACHABLE = "db_unreachable"
MIGRATION_MISMATCH = "migration_mismatch"


@lru_cache(maxsize=1)
def expected_heads() -> frozenset[str]:
    """Head revision(s) shipped with this code. Read from disk once: the
    scripts can't change under a running process."""
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(ALEMBIC_INI.parent / "alembic"))
    return frozenset(ScriptDirectory.from_config(config).get_heads())


def check(db: Session) -> tuple[bool, str | None]:
    """(ready, reason). reason is None when ready."""
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        return False, DB_UNREACHABLE
    try:
        applied = {row[0] for row in db.execute(text("SELECT version_num FROM alembic_version"))}
    except Exception:
        # No alembic_version table: migrations never ran on this database.
        return False, MIGRATION_MISMATCH
    if applied != set(expected_heads()):
        return False, MIGRATION_MISMATCH
    return True, None
