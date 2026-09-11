"""money_movements.doc_date + TaskLinkType.MONEY_MOVEMENT_BACKFILL

Revision ID: b3e8d1f6a204
Revises: a1f4c93e27b0
Create Date: 2026-09-11 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3e8d1f6a204'
down_revision: Union[str, None] = 'a1f4c93e27b0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Задача 0011-k: импорт платежей выпиской.
    op.add_column(
        'money_movements',
        sa.Column('doc_date', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        'ix_money_movements_doc_date', 'money_movements', ['doc_date']
    )
    # Новое значение пользовательского типа-enum. Autogenerate его не видит,
    # добавляем руками; IF NOT EXISTS — идемпотентно; в этой миграции значение
    # не используется, поэтому PG допускает ADD VALUE здесь.
    op.execute(
        "ALTER TYPE task_link_type ADD VALUE IF NOT EXISTS 'MONEY_MOVEMENT_BACKFILL'"
    )


def downgrade() -> None:
    op.drop_index('ix_money_movements_doc_date', table_name='money_movements')
    op.drop_column('money_movements', 'doc_date')
    # Postgres не умеет DROP VALUE для enum — 'MONEY_MOVEMENT_BACKFILL'
    # намеренно остаётся на типе.
