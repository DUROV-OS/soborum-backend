"""clients.lead_unprocessed: подсветка необработанной заявки с сайта (0111-b)

Revision ID: d8e3b9a52c17
Revises: c7d2a8f4e19b
Create Date: 2026-10-18 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd8e3b9a52c17'
down_revision: Union[str, None] = 'c7d2a8f4e19b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('clients', sa.Column('lead_unprocessed', sa.Boolean(), server_default='false', nullable=False))


def downgrade() -> None:
    op.drop_column('clients', 'lead_unprocessed')
