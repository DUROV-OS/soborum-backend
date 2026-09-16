"""add production stage template tables (0066-d)

Revision ID: a7c2e9f13d68
Revises: f4a91c7d0e26
Create Date: 2026-09-17 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7c2e9f13d68'
down_revision: Union[str, None] = 'f4a91c7d0e26'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'production_stage_templates',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('house_model_key', sa.String(length=64), nullable=True),
        sa.Column(
            'status',
            sa.Enum('draft', 'reviewed', 'confirmed', name='production_stage_template_status'),
            nullable=False,
        ),
        sa.Column('source_client_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('confirmed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('confirmed_by_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['house_model_key'], ['house_model_cards.key']),
        sa.ForeignKeyConstraint(['source_client_id'], ['clients.id']),
        sa.ForeignKeyConstraint(['confirmed_by_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table(
        'template_blocks',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('template_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('sequence', sa.Integer(), nullable=False),
        sa.Column('kr_page_refs', sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(['template_id'], ['production_stage_templates.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table(
        'template_block_dependencies',
        sa.Column('block_id', sa.Integer(), nullable=False),
        sa.Column('depends_on_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['block_id'], ['template_blocks.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['depends_on_id'], ['template_blocks.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('block_id', 'depends_on_id'),
    )

    op.create_table(
        'template_block_tasks',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('template_block_id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('kr_page_ref', sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(['template_block_id'], ['template_blocks.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table(
        'template_block_materials',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('template_block_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('unit', sa.String(length=32), nullable=False),
        sa.Column('kr_page_ref', sa.JSON(), nullable=True),
        sa.Column('warehouse_material_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['template_block_id'], ['template_blocks.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['warehouse_material_id'], ['warehouse_materials.id']),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('template_block_materials')
    op.drop_table('template_block_tasks')
    op.drop_table('template_block_dependencies')
    op.drop_table('template_blocks')
    op.drop_table('production_stage_templates')
    op.execute("DROP TYPE production_stage_template_status")
