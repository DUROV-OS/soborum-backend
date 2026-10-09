"""task deadline alerts (0080-c)

Revision ID: 1650c30602fe
Revises: c1a9f8e2b4d6, 52826825a4c1
Create Date: 2026-10-09 00:00:00.000000

Merge-ревизия: на стейдже 0080-a (client_manager) и 0080-b (notifications)
легли как две отдельные головы от 205d05997209 (cherry-pick коммитов, не
merge веток) — в dev-worktree был отдельный merge-коммит с ревизией
9f56791baca5, которой на стейдже нет; здесь down_revision указывает прямо
на обе головы.

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '1650c30602fe'
down_revision: Union[str, tuple[str, ...], None] = ('c1a9f8e2b4d6', '52826825a4c1')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_THRESHOLDS = ['DUE_SOON', 'OVERDUE']


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == 'postgresql'

    threshold_col: sa.types.TypeEngine = (
        postgresql.ENUM(*_THRESHOLDS, name='task_deadline_threshold')
        if is_pg
        else sa.Enum(*_THRESHOLDS, name='task_deadline_threshold')
    )

    op.create_table(
        'task_deadline_alerts',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('task_id', sa.Integer(), nullable=False),
        sa.Column('threshold', threshold_col, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('task_id', 'threshold', name='uq_task_deadline_alert'),
    )
    op.create_index(op.f('ix_task_deadline_alerts_task_id'), 'task_deadline_alerts', ['task_id'])


def downgrade() -> None:
    op.drop_index(op.f('ix_task_deadline_alerts_task_id'), table_name='task_deadline_alerts')
    op.drop_table('task_deadline_alerts')
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        op.execute('DROP TYPE IF EXISTS task_deadline_threshold')
