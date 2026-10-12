"""merge push subscriptions and task deadline alerts

Revision ID: d4e8b1c7a925
Revises: a3f7c2e9b614, 1650c30602fe
Create Date: 2026-10-12 12:30:00.000000

"""
from typing import Sequence, Union


# revision identifiers, used by Alembic.
revision: str = 'd4e8b1c7a925'
down_revision: Union[str, None] = ('a3f7c2e9b614', '1650c30602fe')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
