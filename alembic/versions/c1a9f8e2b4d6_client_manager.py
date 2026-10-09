"""ответственный менеджер клиента (0080-a)

Revision ID: c1a9f8e2b4d6
Revises: 205d05997209
Create Date: 2026-10-09 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c1a9f8e2b4d6'
down_revision: Union[str, None] = '205d05997209'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Nullable, без backfill: клиент без назначенного менеджера — просто нет
    # адресата уведомлений по нему (0080-a).
    op.add_column('clients', sa.Column('manager_id', sa.Integer(), nullable=True))
    op.create_foreign_key(
        'fk_clients_manager_id',
        'clients',
        'users',
        ['manager_id'],
        ['id'],
    )
    op.create_index('ix_clients_manager_id', 'clients', ['manager_id'])


def downgrade() -> None:
    op.drop_index('ix_clients_manager_id', table_name='clients')
    op.drop_constraint('fk_clients_manager_id', 'clients', type_='foreignkey')
    op.drop_column('clients', 'manager_id')
