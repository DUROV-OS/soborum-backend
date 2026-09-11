"""warehouse write-off: WRITE_OFF reason + free-text note on stock_movements

Revision ID: 514e345cbed3
Revises: c1a9e7f04d22
Create Date: 2026-09-11 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '514e345cbed3'
down_revision: Union[str, None] = 'c1a9e7f04d22'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE stock_movement_reason ADD VALUE IF NOT EXISTS 'WRITE_OFF'")
    op.add_column('stock_movements', sa.Column('note', sa.String(length=500), nullable=True))


def downgrade() -> None:
    op.drop_column('stock_movements', 'note')
    # Postgres has no DROP VALUE for enums, so 'WRITE_OFF' is intentionally left on the type.
