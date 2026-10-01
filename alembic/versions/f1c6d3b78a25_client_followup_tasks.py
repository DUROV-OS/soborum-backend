"""task link type client_followup + report kind deadline_shift (0079-d)

Revision ID: f1c6d3b78a25
Revises: e7b3c5a29f14
Create Date: 2026-09-23 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'f1c6d3b78a25'
# Ветки задачи 0079 уходят в main одной приёмкой, поэтому миграции выстроены
# в цепочку (0079-a -> 0079-c -> 0079-d), а не веером от общего предка: две
# головы alembic — это неподнявшийся бэкенд в проде.
down_revision: Union[str, None] = 'e7b3c5a29f14'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SQLAlchemy хранит имена членов enum, отсюда верхний регистр.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE task_link_type ADD VALUE IF NOT EXISTS 'CLIENT_FOLLOWUP'")
        op.execute("ALTER TYPE task_report_kind ADD VALUE IF NOT EXISTS 'DEADLINE_SHIFT'")


def downgrade() -> None:
    # Значения enum в PostgreSQL не удаляются. Задачи по клиенту и записи о
    # переносе срока при откате остаются, но старому коду они не встретятся:
    # он их просто не запрашивает.
    pass
