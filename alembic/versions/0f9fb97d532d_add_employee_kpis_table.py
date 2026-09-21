"""add employee_kpis table

Revision ID: 0f9fb97d532d
Revises: 19c9bcb0f3e3
Create Date: 2026-09-20 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0f9fb97d532d'
down_revision: Union[str, None] = '19c9bcb0f3e3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'employee_kpis',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('employee_id', sa.Integer(), nullable=False),
        sa.Column('period_start', sa.Date(), nullable=False),
        sa.Column('period_end', sa.Date(), nullable=False),
        sa.Column('tasks_total', sa.Integer(), nullable=False),
        sa.Column('tasks_on_time', sa.Integer(), nullable=False),
        sa.Column('tasks_late', sa.Integer(), nullable=False),
        sa.Column('tasks_overdue', sa.Integer(), nullable=False),
        sa.Column('kpi', sa.Integer(), nullable=True),
        sa.Column('computed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['employee_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('employee_id', 'period_start', name='uq_employee_kpi_period'),
    )


def downgrade() -> None:
    op.drop_table('employee_kpis')
