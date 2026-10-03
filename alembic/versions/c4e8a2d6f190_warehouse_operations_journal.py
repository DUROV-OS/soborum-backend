"""документ операции склада, остаток после движения, причины оприходования и отпуска (0088-a)

Revision ID: c4e8a2d6f190
Revises: b8d4f20a6c31
Create Date: 2026-10-03 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c4e8a2d6f190'
down_revision: Union[str, None] = 'b8d4f20a6c31'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ADD VALUE вне транзакции миграции — как в c9a1e5b73f28_client_payment_plan.
    with op.get_context().autocommit_block():
        for value in ('RECEIPT', 'ISSUED_TECHCARD', 'ISSUED_MANUAL'):
            op.execute(f"ALTER TYPE stock_movement_reason ADD VALUE IF NOT EXISTS '{value}'")

    op.create_table(
        'warehouse_operations',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column(
            'kind',
            sa.Enum('RECEIPT', 'WRITE_OFF', 'ISSUE_TECHCARD', 'ISSUE_MANUAL', name='warehouse_operation_kind'),
            nullable=False,
        ),
        sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            'destination_kind',
            sa.Enum('HOUSE', 'OBJECT', 'WORKSHOP', 'REWORK', name='issue_destination_kind'),
            nullable=True,
        ),
        sa.Column('destination', sa.String(length=255), nullable=True),
        sa.Column('production_id', sa.Integer(), sa.ForeignKey('productions.id', ondelete='SET NULL'), nullable=True),
        sa.Column('received_by', sa.String(length=255), nullable=True),
        sa.Column('note', sa.String(length=500), nullable=True),
        sa.Column('created_by_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.add_column(
        'stock_movements',
        sa.Column(
            'operation_id',
            sa.Integer(),
            sa.ForeignKey('warehouse_operations.id', ondelete='RESTRICT'),
            nullable=True,
        ),
    )
    op.add_column('stock_movements', sa.Column('balance_after', sa.Numeric(14, 3), nullable=True))
    op.create_index('ix_stock_movements_operation_id', 'stock_movements', ['operation_id'])


def downgrade() -> None:
    op.drop_index('ix_stock_movements_operation_id', table_name='stock_movements')
    op.drop_column('stock_movements', 'balance_after')
    op.drop_column('stock_movements', 'operation_id')
    op.drop_table('warehouse_operations')
    sa.Enum(name='issue_destination_kind').drop(op.get_bind(), checkfirst=True)
    sa.Enum(name='warehouse_operation_kind').drop(op.get_bind(), checkfirst=True)
    # Postgres has no DROP VALUE for enums, so the new stock_movement_reason values stay on the type.
