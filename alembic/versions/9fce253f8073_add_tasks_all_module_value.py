"""add tasks_all module value

Revision ID: 9fce253f8073
Revises: c4a8f2e17d90
Create Date: 2026-09-11 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '9fce253f8073'
down_revision: Union[str, None] = 'c4a8f2e17d90'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # New enum value on the existing pg `module` type (same pattern as the
    # ACCOUNTING/AI additions - autogenerate doesn't detect added enum values).
    # Pseudo-section: only used as an access-grant flag for the "Все задачи"
    # sub-tab in app.tasks (task 0021), has no API app of its own.
    op.execute("ALTER TYPE module ADD VALUE IF NOT EXISTS 'TASKS_ALL'")


def downgrade() -> None:
    # Postgres has no DROP VALUE for enums, so 'TASKS_ALL' is intentionally
    # left on the type.
    pass
