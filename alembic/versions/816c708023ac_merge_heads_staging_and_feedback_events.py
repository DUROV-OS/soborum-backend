"""merge heads staging and feedback_events (0090)

Revision ID: 816c708023ac
Revises: a9e1f4c3b720, c3e8a1f05b72
Create Date: 2026-10-04 01:50:00.224251

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '816c708023ac'
down_revision: Union[str, None] = ('a9e1f4c3b720', 'c3e8a1f05b72')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
