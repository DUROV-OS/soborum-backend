"""agent_shift_items.has_live_data: отличать реальный вывод от честного «нет данных»

Revision ID: e2f9a4c7b108
Revises: d1e7b9a4c605
Create Date: 2026-09-09 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e2f9a4c7b108"
down_revision: Union[str, None] = "d1e7b9a4c605"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # False — по строке нет живого источника и Claude не ответил: stance это
    # честное «нет данных», а не анализ. Старые строки трактуем как «нет данных».
    op.add_column(
        "agent_shift_items",
        sa.Column("has_live_data", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("agent_shift_items", "has_live_data")
