"""merge heads staging and warehouse material flow (0088)

Revision ID: e5a9c2f7b310
Revises: 816c708023ac, d7b3f9e1a254
Create Date: 2026-10-05 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5a9c2f7b310'
down_revision: Union[str, None] = ('816c708023ac', 'd7b3f9e1a254')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
