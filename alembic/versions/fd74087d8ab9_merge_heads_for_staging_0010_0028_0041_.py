"""merge heads for staging (0010, 0028, 0041/0042, 0073-a, 0073-b)

Revision ID: fd74087d8ab9
Revises: 0f9fb97d532d, 1b47820961ec, 3fb5651a7a81, 476439581e96
Create Date: 2026-09-21 16:07:27.942294

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'fd74087d8ab9'
down_revision: Union[str, None] = ('0f9fb97d532d', '1b47820961ec', '3fb5651a7a81', '476439581e96')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
