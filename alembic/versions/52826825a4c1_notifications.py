"""notifications and notification mutes (0080-b)

Revision ID: 52826825a4c1
Revises: 205d05997209
Create Date: 2026-10-09 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '52826825a4c1'
down_revision: Union[str, None] = '205d05997209'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NOTIFICATION_KINDS = [
    'TASK_DUE',
    'TASK_OVERDUE',
    'CLIENT_UPDATE',
    'PRODUCTION_UPDATE',
    'INSTALLATION_UPDATE',
    'MATERIAL_REQUEST_UPDATE',
    'MONEY_MOVEMENT_UPDATE',
    'CONTENT_UPDATE',
    'MAX_MESSAGE',
    'FEEDBACK_UPDATE',
]


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == 'postgresql'

    kind_col: sa.types.TypeEngine = (
        postgresql.ENUM(*_NOTIFICATION_KINDS, name='notification_kind')
        if is_pg
        else sa.Enum(*_NOTIFICATION_KINDS, name='notification_kind')
    )
    # Тип module уже существует (8d7c2ac1bb85_init, UserModuleAccess) —
    # переиспользуем его, не создаём заново (тот же приём, что в
    # a9267e2c5497_client_chat_links.py).
    module_col: sa.types.TypeEngine = (
        postgresql.ENUM(name='module', create_type=False)
        if is_pg
        else sa.Enum(name='module')
    )

    op.create_table(
        'notifications',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('kind', kind_col, nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('body', sa.Text(), nullable=True),
        sa.Column('object_type', sa.String(length=64), nullable=True),
        sa.Column('object_id', sa.BigInteger(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('read_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_notifications_user_id'), 'notifications', ['user_id'])
    op.create_index(op.f('ix_notifications_created_at'), 'notifications', ['created_at'])

    op.create_table(
        'notification_mutes',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('module', module_col, nullable=False),
        sa.Column('object_id', sa.BigInteger(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'module', 'object_id', name='uq_notification_mute'),
    )
    op.create_index(op.f('ix_notification_mutes_user_id'), 'notification_mutes', ['user_id'])


def downgrade() -> None:
    op.drop_index(op.f('ix_notification_mutes_user_id'), table_name='notification_mutes')
    op.drop_table('notification_mutes')
    op.drop_index(op.f('ix_notifications_created_at'), table_name='notifications')
    op.drop_index(op.f('ix_notifications_user_id'), table_name='notifications')
    op.drop_table('notifications')
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        op.execute('DROP TYPE IF EXISTS notification_kind')
