"""add house_model_cards table and house_models module value

Revision ID: f95f9e24ee18
Revises: 514e345cbed3
Create Date: 2026-09-12 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f95f9e24ee18'
down_revision: Union[str, None] = '514e345cbed3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # New Module.HOUSE_MODELS access-grant value. Autogenerate doesn't detect
    # added enum values on an existing pg enum type, so this is added by hand
    # (see f030f9e91731 for the same precedent with Module.AI).
    op.execute("ALTER TYPE module ADD VALUE IF NOT EXISTS 'HOUSE_MODELS'")

    op.create_table(
        'house_model_cards',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('key', sa.String(length=64), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('kind', sa.Enum('CATALOG', 'INDIVIDUAL', name='house_model_kind'), nullable=False),
        sa.Column('series', sa.String(length=32), nullable=True),
        sa.Column('area_footprint_m2', sa.Float(), nullable=True),
        sa.Column('area_total_m2', sa.Float(), nullable=True),
        sa.Column('price_site_rub', sa.Integer(), nullable=True),
        sa.Column('deal_amount_rub', sa.Integer(), nullable=True),
        sa.Column('client_name', sa.String(length=255), nullable=True),
        sa.Column(
            'confirmation',
            sa.Enum('CONFIRMED', 'PARTIAL', 'NONE', name='house_model_confirmation'),
            nullable=False,
        ),
        sa.Column('confirmation_label', sa.String(length=255), nullable=False),
        sa.Column('source_note_path', sa.String(length=255), nullable=False),
        sa.Column('characteristics_md', sa.Text(), nullable=True),
        sa.Column('planning_md', sa.Text(), nullable=True),
        sa.Column('configurations_md', sa.Text(), nullable=True),
        sa.Column('modules_md', sa.Text(), nullable=True),
        sa.Column('economics_md', sa.Text(), nullable=True),
        sa.Column('production_experience_md', sa.Text(), nullable=True),
        sa.Column('deals_without_pz_md', sa.Text(), nullable=True),
        sa.Column('files_md', sa.Text(), nullable=True),
        sa.Column('open_questions_md', sa.Text(), nullable=True),
        sa.Column('notes_md', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_house_model_cards_key'), 'house_model_cards', ['key'], unique=True)


def downgrade() -> None:
    op.drop_index(op.f('ix_house_model_cards_key'), table_name='house_model_cards')
    op.drop_table('house_model_cards')
    sa.Enum(name='house_model_kind').drop(op.get_bind(), checkfirst=True)
    sa.Enum(name='house_model_confirmation').drop(op.get_bind(), checkfirst=True)
    # Note: Postgres has no DROP VALUE for enums, so the 'HOUSE_MODELS' value
    # added to the `module` type in upgrade() is intentionally left in place.
