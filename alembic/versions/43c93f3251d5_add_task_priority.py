"""add task priority

Revision ID: 43c93f3251d5
Revises: a9267e2c5497
Create Date: 2026-09-18 05:17:13.310064

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '43c93f3251d5'
down_revision: Union[str, None] = 'a9267e2c5497'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    task_priority = sa.Enum('LOW', 'MEDIUM', 'HIGH', name='task_priority')
    task_priority.create(op.get_bind(), checkfirst=True)

    op.add_column(
        'tasks',
        sa.Column('priority', task_priority, nullable=False, server_default='MEDIUM'),
    )


def downgrade() -> None:
    op.drop_column('tasks', 'priority')
    sa.Enum(name='task_priority').drop(op.get_bind(), checkfirst=True)
