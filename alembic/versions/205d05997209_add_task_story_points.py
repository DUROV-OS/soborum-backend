"""add task story points

Revision ID: 205d05997209
Revises: 43c93f3251d5
Create Date: 2026-09-18 05:31:08.811065

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '205d05997209'
down_revision: Union[str, None] = '43c93f3251d5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Без default: старые задачи остаются NULL до разового backfill
    # (app/tasks/story_points_backfill.py), а не получают выдуманное значение.
    op.add_column('tasks', sa.Column('story_points', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('tasks', 'story_points')
