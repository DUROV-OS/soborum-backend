"""add template_material_mappings table + confidence on template_block_materials (0073-a)

Revision ID: 1b47820961ec
Revises: 19c9bcb0f3e3
Create Date: 2026-09-20 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1b47820961ec'
down_revision: Union[str, None] = '19c9bcb0f3e3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    mapping_confidence = sa.Enum('high', 'medium', name='mapping_confidence')
    mapping_confidence.create(op.get_bind(), checkfirst=True)

    op.create_table(
        'template_material_mappings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('normalized_name', sa.String(length=255), nullable=False),
        sa.Column('unit', sa.String(length=32), nullable=False),
        sa.Column('warehouse_material_id', sa.Integer(), nullable=False),
        sa.Column('confidence', mapping_confidence, nullable=False),
        sa.Column('matched_by', sa.Enum('ai', 'human', name='mapping_matched_by'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['warehouse_material_id'], ['warehouse_materials.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('normalized_name', 'unit', name='uq_template_material_mappings_name_unit'),
    )

    # Уверенность автосопоставления материала шаблона со складом (0073-a) —
    # NULL для уже существующих строк (сопоставлены вручную давно или ещё не
    # сопоставлены), новые генерации/backfill заполняют его сами.
    op.add_column(
        'template_block_materials',
        sa.Column('confidence', mapping_confidence, nullable=True),
    )


def downgrade() -> None:
    op.drop_column('template_block_materials', 'confidence')
    op.drop_table('template_material_mappings')
    sa.Enum(name='mapping_matched_by').drop(op.get_bind(), checkfirst=True)
    sa.Enum(name='mapping_confidence').drop(op.get_bind(), checkfirst=True)
