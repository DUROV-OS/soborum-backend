"""merge heads: employee kpis and feedback requests

Revision ID: 2d83291bce19
Revises: 0f9fb97d532d, a7c1e4b95d30
Create Date: 2026-09-21 17:21:22.480859

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2d83291bce19'
down_revision: Union[str, None] = ('0f9fb97d532d', 'a7c1e4b95d30')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
