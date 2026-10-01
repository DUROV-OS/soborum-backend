"""task report kind reviewer_assigned (0084-f)

Revision ID: a0726ebb1e8d
Revises: b3f8d1e6c527
Create Date: 2026-09-29 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'a0726ebb1e8d'
# Миграции 0084 выстроены цепочкой в порядке мержа: эта идёт после 0084-e.
down_revision: Union[str, None] = 'b3f8d1e6c527'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SQLAlchemy хранит имена членов enum, отсюда верхний регистр.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE task_report_kind ADD VALUE IF NOT EXISTS 'REVIEWER_ASSIGNED'")


def downgrade() -> None:
    # Значения enum в PostgreSQL не удаляются. Записи о назначении
    # проверяющего при откате остаются в журнале, старый код их просто не
    # создаёт.
    pass
