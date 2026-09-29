"""оценка готовности производства (0084-b): requires_materials у блока и шаблона,
link_type BLOCK_MATERIAL_MATCH, время правки блока и материала

Revision ID: e7b41c9d2f10
Revises: d5e2a90c1b77
Create Date: 2026-09-29 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e7b41c9d2f10'
# Параллельные ветки 0084 (e, g, i, j) тоже режутся от d5e2a90c1b77: при мерже
# после них down_revision нужно перевесить на их голову — две головы alembic
# означают неподнявшийся бэкенд в проде.
down_revision: Union[str, None] = 'd5e2a90c1b77'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE task_link_type ADD VALUE IF NOT EXISTS 'BLOCK_MATERIAL_MATCH'")

    op.add_column(
        'production_blocks',
        sa.Column('requires_materials', sa.Boolean(), server_default=sa.true(), nullable=False),
    )
    # Без server_default: у существующих строк время правки неизвестно, и
    # «сейчас» было бы выдуманным временем факта.
    op.add_column('production_blocks', sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('block_materials', sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        'template_blocks',
        sa.Column('requires_materials', sa.Boolean(), server_default=sa.true(), nullable=False),
    )

    # Открытые задачи «Сопоставить со складом материал…», созданные
    # app/production/stage_plan.py до появления отдельного link_type.
    # Значение enum добавлено выше в autocommit-блоке, поэтому здесь его уже
    # можно использовать.
    op.execute(
        """
        UPDATE tasks
        SET link_type = 'BLOCK_MATERIAL_MATCH', link_id = block_id
        WHERE block_id IS NOT NULL
          AND link_type = 'NONE'
          AND status <> 'DONE'
          AND title LIKE 'Сопоставить со складом материал%'
        """
    )


def downgrade() -> None:
    op.execute(
        "UPDATE tasks SET link_type = 'NONE', link_id = NULL WHERE link_type = 'BLOCK_MATERIAL_MATCH'"
    )
    op.drop_column('template_blocks', 'requires_materials')
    op.drop_column('block_materials', 'updated_at')
    op.drop_column('production_blocks', 'updated_at')
    op.drop_column('production_blocks', 'requires_materials')
    # PostgreSQL не умеет удалять значения из enum; лишнее значение безвредно.
