"""add Task.responsible_id (0066-g)

Revision ID: c3e7b1a94f52
Revises: 86e1699c8226
Create Date: 2026-09-17 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3e7b1a94f52'
down_revision: Union[str, None] = 'a7c2e9f13d68'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('tasks', sa.Column('responsible_id', sa.Integer(), nullable=True))
    op.create_foreign_key(
        'fk_tasks_responsible_id_users', 'tasks', 'users', ['responsible_id'], ['id']
    )


def downgrade() -> None:
    op.drop_constraint('fk_tasks_responsible_id_users', 'tasks', type_='foreignkey')
    op.drop_column('tasks', 'responsible_id')
