"""add meeting topic and goals

Revision ID: a3e7b1c5d208
Revises: f2d4a6c8e015
Create Date: 2026-09-10 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3e7b1c5d208'
down_revision: Union[str, None] = 'f2d4a6c8e015'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('ai_meetings', sa.Column('topic', sa.String(length=255), nullable=True))
    op.add_column('ai_meetings', sa.Column('goals', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('ai_meetings', 'goals')
    op.drop_column('ai_meetings', 'topic')
