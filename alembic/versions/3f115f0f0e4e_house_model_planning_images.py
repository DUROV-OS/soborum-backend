"""add planning_image_id to house_model_cards + file_purpose value

Revision ID: 3f115f0f0e4e
Revises: f95f9e24ee18
Create Date: 2026-09-12 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3f115f0f0e4e'
down_revision: Union[str, None] = 'f95f9e24ee18'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # New FilePurpose.HOUSE_MODEL_PLANNING value (see f030f9e91731/f95f9e24ee18
    # for the same precedent adding a value to an existing pg enum type).
    op.execute("ALTER TYPE file_purpose ADD VALUE IF NOT EXISTS 'HOUSE_MODEL_PLANNING'")

    op.add_column('house_model_cards', sa.Column('planning_image_id', sa.Integer(), nullable=True))
    op.create_foreign_key(
        'fk_house_model_cards_planning_image_id_file_assets',
        'house_model_cards', 'file_assets',
        ['planning_image_id'], ['id'],
    )


def downgrade() -> None:
    op.drop_constraint('fk_house_model_cards_planning_image_id_file_assets', 'house_model_cards', type_='foreignkey')
    op.drop_column('house_model_cards', 'planning_image_id')
    # Note: Postgres has no DROP VALUE for enums, so the 'HOUSE_MODEL_PLANNING'
    # value added to `file_purpose` in upgrade() is intentionally left in place.
