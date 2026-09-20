"""add typical_architectural_decisions/typical_constructive_decisions file purpose values

Revision ID: 7647843c5a8a
Revises: 19c9bcb0f3e3
Create Date: 2026-09-20 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '7647843c5a8a'
down_revision: Union[str, None] = '19c9bcb0f3e3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # New FilePurpose values for the typical АР/КР of a catalog house model
    # (0073-b) — a model-wide sample, not tied to any client. Autogenerate
    # doesn't detect added enum values on an existing pg enum type, so this is
    # added by hand (same pattern as dc24540a19b2 for MONEY_MOVEMENT_DOCUMENT).
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE file_purpose ADD VALUE IF NOT EXISTS 'TYPICAL_ARCHITECTURAL_DECISIONS'")
        op.execute("ALTER TYPE file_purpose ADD VALUE IF NOT EXISTS 'TYPICAL_CONSTRUCTIVE_DECISIONS'")


def downgrade() -> None:
    # Postgres has no DROP VALUE for enums; the added values are intentionally
    # left in place here.
    pass
