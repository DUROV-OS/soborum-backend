"""push subscriptions: браузерная подписка на web-push (0080-e)

Revision ID: a3f7c2e9b614
Revises: 52826825a4c1
Create Date: 2026-10-09 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a3f7c2e9b614'
down_revision: Union[str, None] = '52826825a4c1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'push_subscriptions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('endpoint', sa.Text(), nullable=False),
        sa.Column('p256dh', sa.String(length=255), nullable=False),
        sa.Column('auth', sa.String(length=255), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('endpoint', name='uq_push_subscription_endpoint'),
    )
    op.create_index(op.f('ix_push_subscriptions_user_id'), 'push_subscriptions', ['user_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_push_subscriptions_user_id'), table_name='push_subscriptions')
    op.drop_table('push_subscriptions')
