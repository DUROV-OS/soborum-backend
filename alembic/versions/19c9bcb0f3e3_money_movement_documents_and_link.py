"""money_movement_documents m2m + MoneyMovement.link (0072-d)

Revision ID: 19c9bcb0f3e3
Revises: dc24540a19b2
Create Date: 2026-09-18 00:00:01.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '19c9bcb0f3e3'
down_revision: Union[str, None] = 'dc24540a19b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'money_movement_documents',
        sa.Column('money_movement_id', sa.Integer(), nullable=False),
        sa.Column('file_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['file_id'], ['file_assets.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['money_movement_id'], ['money_movements.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('money_movement_id', 'file_id'),
    )
    op.add_column('money_movements', sa.Column('link', sa.String(length=500), nullable=True))


def downgrade() -> None:
    op.drop_column('money_movements', 'link')
    op.drop_table('money_movement_documents')
