"""agent_shift_items.stance_source: откуда позиция роли — живой факт, Claude без фактов или ничего (0084-e)

Revision ID: a7c4e9f2b813
Revises: e7b41c9d2f10
Create Date: 2026-09-29 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7c4e9f2b813"
# Миграции 0084 выстроены цепочкой в порядке мержа: эта идёт после 0084-b.
down_revision: Union[str, None] = "e7b41c9d2f10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # live / llm_without_facts / none. Старые строки — NULL: источник не
    # записывался, а прежний has_live_data=True мог означать «Claude ответил».
    op.add_column("agent_shift_items", sa.Column("stance_source", sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_shift_items", "stance_source")
