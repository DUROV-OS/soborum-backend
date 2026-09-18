"""merge heads: 0072-d money movement documents + 0070-d story points

Revision ID: 3fb5651a7a81
Revises: 19c9bcb0f3e3, 205d05997209
Create Date: 2026-09-18 07:02:32.226920

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3fb5651a7a81'
down_revision: Union[str, None] = ('19c9bcb0f3e3', '205d05997209')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
