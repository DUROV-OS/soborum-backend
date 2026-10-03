"""норматив количества материала на дом в шаблоне КР (0088-e)

Revision ID: d7b3f9e1a254
Revises: c4e8a2d6f190
Create Date: 2026-10-03 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd7b3f9e1a254'
# Цепочкой после 0088-a — обе миграции иначе ответвлялись бы от одной головы.
down_revision: Union[str, None] = 'c4e8a2d6f190'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # У существующих шаблонов норматива нет — NULL, «в КР не найдено».
    op.add_column('template_block_materials', sa.Column('quantity', sa.Numeric(14, 3), nullable=True))


def downgrade() -> None:
    op.drop_column('template_block_materials', 'quantity')
