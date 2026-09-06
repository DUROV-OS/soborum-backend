"""task stage time tracking: task_stage_events log + task_work_durations rollup

Revision ID: a7f3c1e9d204
Revises: e4b2d7f36a19
Create Date: 2026-09-06 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'a7f3c1e9d204'
down_revision: Union[str, None] = 'e4b2d7f36a19'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Append-only audit of every task status change (feeds employee KPI and the
    # AI task-duration estimator) plus a derived per-task rollup. Nothing about
    # the Tasks API or task behaviour changes - these tables are written as a
    # side effect by app.tasks.timelog.
    #
    # Reuse the existing PG enum type created in the init migration; do NOT
    # recreate or drop it, it is shared with tasks.status. create_type=False is
    # a PostgreSQL-dialect option, so the enum must be postgresql.ENUM - the
    # generic sa.Enum silently ignores the flag and re-emits CREATE TYPE.
    task_status = postgresql.ENUM(
        'NOT_READY', 'READY', 'IN_PROGRESS', 'IN_REVIEW', 'DONE',
        name='task_status', create_type=False,
    )

    op.create_table(
        'task_stage_events',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('task_id', sa.Integer(), nullable=False),
        sa.Column('from_status', task_status, nullable=True),
        sa.Column('to_status', task_status, nullable=False),
        sa.Column('actor_id', sa.Integer(), nullable=True),
        sa.Column('automatic', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('note', sa.String(length=255), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['actor_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_task_stage_events_task_id', 'task_stage_events', ['task_id'])
    op.create_index('ix_task_stage_events_created_at', 'task_stage_events', ['created_at'])

    op.create_table(
        'task_work_durations',
        sa.Column('task_id', sa.Integer(), nullable=False),
        sa.Column('first_started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('total_working_seconds', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('total_lead_seconds', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('seconds_by_status', sa.JSON(), nullable=False, server_default='{}'),
        sa.Column('is_complete', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('task_id'),
    )


def downgrade() -> None:
    op.drop_table('task_work_durations')
    op.drop_index('ix_task_stage_events_created_at', table_name='task_stage_events')
    op.drop_index('ix_task_stage_events_task_id', table_name='task_stage_events')
    op.drop_table('task_stage_events')
