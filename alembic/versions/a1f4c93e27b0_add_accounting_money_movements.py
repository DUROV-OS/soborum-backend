"""add accounting module value and money_movements table

Revision ID: a1f4c93e27b0
Revises: d2f6a1b4e390
Create Date: 2026-09-11 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1f4c93e27b0'
down_revision: Union[str, None] = 'd2f6a1b4e390'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # New enum values on existing pg enum types. Autogenerate doesn't detect
    # added values, so these are done by hand (same as the Module.AI migration
    # f030f9e91731). IF NOT EXISTS keeps it idempotent; the values are not used
    # elsewhere in this migration, so PG allows ADD VALUE here.
    op.execute("ALTER TYPE module ADD VALUE IF NOT EXISTS 'ACCOUNTING'")
    op.execute("ALTER TYPE task_link_type ADD VALUE IF NOT EXISTS 'MONEY_MOVEMENT_APPROVAL'")

    money_direction = sa.Enum('INCOME', 'EXPENSE', name='money_direction')
    money_subkind = sa.Enum(
        'SALE_INCOME', 'SALARY_PAYOUT', 'SUPPLY_PAYMENT', 'TAX', 'RENT',
        'OTHER_INCOME', 'OTHER_EXPENSE', name='money_subkind',
    )
    money_assessment = sa.Enum('PLANNED', 'ACTUAL', name='money_assessment')
    money_movement_status = sa.Enum(
        'DRAFT', 'APPROVED', 'POSTED', 'CANCELLED', name='money_movement_status'
    )
    money_source_kind = sa.Enum('NONE', 'CLIENT', 'EMPLOYEE', 'SUPPLY', name='money_source_kind')

    op.create_table(
        'money_movements',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('direction', money_direction, nullable=False),
        sa.Column('subkind', money_subkind, nullable=False),
        sa.Column('amount', sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column('currency', sa.String(length=3), server_default='RUB', nullable=False),
        sa.Column('tax', sa.Numeric(precision=14, scale=2), server_default='0', nullable=False),
        sa.Column('assessment', money_assessment, server_default='ACTUAL', nullable=False),
        sa.Column('affects_profit', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('initiator_id', sa.Integer(), nullable=False),
        sa.Column('status', money_movement_status, server_default='DRAFT', nullable=False),
        sa.Column('posted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('cancel_reason', sa.Text(), nullable=True),
        sa.Column('payment_purpose', sa.String(length=500), nullable=True),
        sa.Column('comment', sa.Text(), nullable=True),
        sa.Column('external_number', sa.String(length=255), nullable=True),
        sa.Column('source_kind', money_source_kind, server_default='NONE', nullable=False),
        sa.Column('client_id', sa.Integer(), nullable=True),
        sa.Column('employee_id', sa.Integer(), nullable=True),
        sa.Column('supply_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['initiator_id'], ['users.id']),
        sa.ForeignKeyConstraint(['client_id'], ['clients.id']),
        sa.ForeignKeyConstraint(['employee_id'], ['users.id']),
        sa.ForeignKeyConstraint(['supply_id'], ['supplies.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_money_movements_status', 'money_movements', ['status'])
    op.create_index('ix_money_movements_subkind', 'money_movements', ['subkind'])
    op.create_index('ix_money_movements_client_id', 'money_movements', ['client_id'])
    op.create_index('ix_money_movements_employee_id', 'money_movements', ['employee_id'])
    op.create_index('ix_money_movements_supply_id', 'money_movements', ['supply_id'])


def downgrade() -> None:
    op.drop_index('ix_money_movements_supply_id', table_name='money_movements')
    op.drop_index('ix_money_movements_employee_id', table_name='money_movements')
    op.drop_index('ix_money_movements_client_id', table_name='money_movements')
    op.drop_index('ix_money_movements_subkind', table_name='money_movements')
    op.drop_index('ix_money_movements_status', table_name='money_movements')
    op.drop_table('money_movements')
    for name in (
        'money_source_kind', 'money_movement_status', 'money_assessment',
        'money_subkind', 'money_direction',
    ):
        sa.Enum(name=name).drop(op.get_bind(), checkfirst=True)
    # Postgres has no DROP VALUE for enums, so the 'ACCOUNTING' and
    # 'MONEY_MOVEMENT_APPROVAL' values are intentionally left on their types.
