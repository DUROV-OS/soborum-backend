"""merge push-подписок с общей цепочкой стейджа

Revision ID: 6edf56c95856
Revises: f2441bfee227, a3f7c2e9b614
Create Date: 2026-10-09 12:15:11.506757

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6edf56c95856'
down_revision: Union[str, None] = ('f2441bfee227', 'a3f7c2e9b614')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
