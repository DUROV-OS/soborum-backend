"""payment_edit_unlocked on clients — admin toggle to bypass payment lock (0054)

Revision ID: 4392ad07a8eb
Revises: d32b9b42091d
Create Date: 2026-09-18 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '4392ad07a8eb'
down_revision: Union[str, None] = 'd32b9b42091d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'clients',
        sa.Column('payment_edit_unlocked', sa.Boolean(), nullable=False, server_default='false'),
    )


def downgrade() -> None:
    op.drop_column('clients', 'payment_edit_unlocked')
