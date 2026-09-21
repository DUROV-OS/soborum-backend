"""merge heads after staging integration (0075-a feedback)

Revision ID: 18acd0e89e98
Revises: 8ed6af43a1db, a7c1e4b95d30
Create Date: 2026-09-21 16:56:20.858247

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '18acd0e89e98'
down_revision: Union[str, None] = ('8ed6af43a1db', 'a7c1e4b95d30')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
