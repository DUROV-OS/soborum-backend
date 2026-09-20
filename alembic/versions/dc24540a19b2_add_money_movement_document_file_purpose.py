"""add money_movement_document file purpose value

Revision ID: dc24540a19b2
Revises: a9267e2c5497
Create Date: 2026-09-18 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'dc24540a19b2'
down_revision: Union[str, None] = 'a9267e2c5497'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # New FilePurpose.MONEY_MOVEMENT_DOCUMENT value, for files attached to a
    # money movement (0072-d). Autogenerate doesn't detect added enum values
    # on an existing pg enum type, so this is added by hand (same pattern as
    # 5b3615d19b4e for FilePurpose.AI_CHAT_ATTACHMENT).
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE file_purpose ADD VALUE IF NOT EXISTS 'MONEY_MOVEMENT_DOCUMENT'")


def downgrade() -> None:
    # Postgres has no DROP VALUE for enums; the added value is intentionally
    # left in place here.
    pass
