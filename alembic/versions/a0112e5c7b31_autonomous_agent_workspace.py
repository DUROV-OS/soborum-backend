"""рабочее пространство автономного агента (0112): журнал, приоритеты, наблюдения,
счётчики повторов; link_type AGENT_PROPOSAL

Revision ID: a0112e5c7b31
Revises: b6c3e9a41f08
Create Date: 2026-10-16 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'a0112e5c7b31'
down_revision: Union[str, None] = 'b6c3e9a41f08'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE task_link_type ADD VALUE IF NOT EXISTS 'AGENT_PROPOSAL'")

    op.create_table(
        'agent_auto_runs',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('trigger', sa.String(16), nullable=False, server_default='schedule'),
        sa.Column('sections', sa.JSON(), nullable=False),
        sa.Column('deviations', sa.JSON(), nullable=False),
        sa.Column('actions', sa.JSON(), nullable=False),
        sa.Column('signals', sa.JSON(), nullable=False),
        sa.Column('summary', sa.Text(), nullable=False, server_default=''),
        sa.Column('error', sa.Text(), nullable=True),
    )
    op.create_index('ix_agent_auto_runs_started_at', 'agent_auto_runs', ['started_at'])
    op.create_table(
        'agent_priorities',
        sa.Column('section', sa.String(32), primary_key=True),
        sa.Column('weight', sa.Integer(), nullable=False, server_default='5'),
        sa.Column('signal_streak', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('quiet_streak', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        'agent_watchlist',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('key', sa.String(128), nullable=False),
        sa.Column('section', sa.String(32), nullable=False),
        sa.Column('scenario', sa.String(32), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('status', sa.String(16), nullable=False, server_default='open'),
        sa.Column('seen_count', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('escalate_after', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('resolved_by_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
    )
    op.create_index('ix_agent_watchlist_key', 'agent_watchlist', ['key'], unique=True)
    op.create_index('ix_agent_watchlist_status', 'agent_watchlist', ['status'])
    op.create_table(
        'agent_action_counters',
        sa.Column('scenario', sa.String(32), primary_key=True),
        sa.Column('repeats', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_action_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_reaction_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    # Значение enum AGENT_PROPOSAL в Postgres не удаляется; безвредно остаётся.
    op.drop_table('agent_action_counters')
    op.drop_index('ix_agent_watchlist_status', table_name='agent_watchlist')
    op.drop_index('ix_agent_watchlist_key', table_name='agent_watchlist')
    op.drop_table('agent_watchlist')
    op.drop_table('agent_priorities')
    op.drop_index('ix_agent_auto_runs_started_at', table_name='agent_auto_runs')
    op.drop_table('agent_auto_runs')
