"""срок оплаты остатка по договору (0084-j)

Revision ID: b8d4f20a6c31
Revises: a7c3e19f4b20
Create Date: 2026-09-29 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b8d4f20a6c31'
# Миграции 0084 выстроены цепочкой в порядке мержа: эта идёт после 0084-i.
down_revision: Union[str, None] = 'a7c3e19f4b20'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Срока у существующих клиентов нет и задним числом его не вывести —
    # NULL, то есть «срок не указан».
    op.add_column("clients", sa.Column("balance_due_date", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("clients", "balance_due_date")
