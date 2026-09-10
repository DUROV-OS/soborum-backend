"""task_link_type: значение SUPPLIER_PRICE_BACKFILL (задача дозаполнить прайс)

Задача 0011-g. Импорт прайс-листа поставщика таблицей ставит задачу дозаполнить
поля, для которых ИИ не нашёл колонок.

Revision ID: c1d5e9f37a84
Revises: b7e3f1a92c50
Create Date: 2026-09-11 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c1d5e9f37a84'
down_revision: Union[str, None] = 'b7e3f1a92c50'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE task_link_type ADD VALUE IF NOT EXISTS 'SUPPLIER_PRICE_BACKFILL'")


def downgrade() -> None:
    # PostgreSQL не умеет удалять значения из enum; лишнее значение безвредно.
    pass
