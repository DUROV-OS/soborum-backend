"""rename production module -> block, add sequence + dependencies graph

Revision ID: b1c3e7a2f9d4
Revises: 86e1699c8226
Create Date: 2026-09-17 00:00:00.000000

Переименование сущностей раздела «Сборка»: ProductionModule/ModuleMaterial ->
ProductionBlock/BlockMaterial (задача 0066-a). Блок — тот же набор
возможностей, что и модуль (материалы, задачи, заявки на склад), плюс
порядок (`sequence`) и граф зависимостей (`block_dependencies`) между блоками
одного производства.

Переименовывает существующие таблицы/колонки на месте (без пересоздания
данных), затем добавляет новые поле и таблицу.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b1c3e7a2f9d4'
down_revision: Union[str, None] = '86e1699c8226'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.rename_table('modules', 'production_blocks')
    op.rename_table('module_materials', 'block_materials')

    op.alter_column('block_materials', 'module_id', new_column_name='block_id')
    op.alter_column('material_requests', 'module_material_id', new_column_name='block_material_id')
    op.alter_column('tasks', 'module_id', new_column_name='block_id')

    op.add_column(
        'production_blocks',
        sa.Column('sequence', sa.Integer(), nullable=False, server_default='0'),
    )

    op.create_table(
        'block_dependencies',
        sa.Column('block_id', sa.Integer(), nullable=False),
        sa.Column('depends_on_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['block_id'], ['production_blocks.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['depends_on_id'], ['production_blocks.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('block_id', 'depends_on_id'),
    )


def downgrade() -> None:
    op.drop_table('block_dependencies')
    op.drop_column('production_blocks', 'sequence')

    op.alter_column('tasks', 'block_id', new_column_name='module_id')
    op.alter_column('material_requests', 'block_material_id', new_column_name='module_material_id')
    op.alter_column('block_materials', 'block_id', new_column_name='module_id')

    op.rename_table('block_materials', 'module_materials')
    op.rename_table('production_blocks', 'modules')
