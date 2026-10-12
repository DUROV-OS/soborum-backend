"""merge client manager and notifications

Revision ID: 9f56791baca5
Revises: c1a9f8e2b4d6, 52826825a4c1
Create Date: 2026-10-09 11:12:39.870413

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9f56791baca5'
down_revision: Union[str, None] = ('c1a9f8e2b4d6', '52826825a4c1')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
