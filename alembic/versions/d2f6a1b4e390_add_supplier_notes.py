"""supplier_notes: свободные заметки по поставщику

Задача 0011-i. Зеркало client_notes для поставщика.

Revision ID: d2f6a1b4e390
Revises: c1d5e9f37a84
Create Date: 2026-09-11 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd2f6a1b4e390'
down_revision: Union[str, None] = 'c1d5e9f37a84'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'supplier_notes',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('supplier_id', sa.Integer(), nullable=False),
        sa.Column('author_id', sa.Integer(), nullable=False),
        sa.Column('text', sa.String(length=2000), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.ForeignKeyConstraint(['supplier_id'], ['suppliers.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['author_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_supplier_notes_supplier_id', 'supplier_notes', ['supplier_id'])


def downgrade() -> None:
    op.drop_index('ix_supplier_notes_supplier_id', table_name='supplier_notes')
    op.drop_table('supplier_notes')
