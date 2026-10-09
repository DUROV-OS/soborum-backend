"""merge стейджа с веткой уведомлений (0080)

Revision ID: f2441bfee227
Revises: e5a9c2f7b310, 1650c30602fe
Create Date: 2026-10-09 12:12:57.099888

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f2441bfee227'
down_revision: Union[str, None] = ('e5a9c2f7b310', '1650c30602fe')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
