"""client payment plan: full prepayment / advance + balance / post-payment

Revision ID: c9a1e5b73f28
Revises: a7f3c1e9d204
Create Date: 2026-09-06 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c9a1e5b73f28'
down_revision: Union[str, None] = 'a7f3c1e9d204'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Формат расчёта клиента определяет, в какой момент цикла нужны деньги.
    # Все ранее заведённые клиенты — полная предоплата (server_default).
    payment_plan = sa.Enum(
        'FULL_PREPAYMENT', 'ADVANCE_THEN_BALANCE', 'POST_PAYMENT', name='payment_plan'
    )
    payment_plan.create(op.get_bind(), checkfirst=True)

    op.add_column(
        'clients',
        sa.Column(
            'payment_plan', payment_plan, nullable=False, server_default='FULL_PREPAYMENT'
        ),
    )
    op.add_column('clients', sa.Column('advance_amount', sa.Numeric(14, 2), nullable=True))
    op.add_column('clients', sa.Column('balance_paid', sa.Boolean(), nullable=True))
    op.add_column(
        'clients', sa.Column('balance_paid_at', sa.DateTime(timezone=True), nullable=True)
    )
    # Ранее заведённые клиенты дошедшие до оплаты — предоплата уже покрывает остаток.
    op.execute("UPDATE clients SET balance_paid = TRUE WHERE payment_locked_at IS NOT NULL")

    # Новый вид связанной задачи — приём остатка «после получения».
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE task_link_type ADD VALUE IF NOT EXISTS 'CLIENT_BALANCE_PAYMENT'")


def downgrade() -> None:
    op.drop_column('clients', 'balance_paid_at')
    op.drop_column('clients', 'balance_paid')
    op.drop_column('clients', 'advance_amount')
    op.drop_column('clients', 'payment_plan')
    sa.Enum(name='payment_plan').drop(op.get_bind(), checkfirst=True)
    # Значение enum task_link_type не удаляем: PostgreSQL не умеет убирать
    # значения из enum, а на работу лишнее значение не влияет.
