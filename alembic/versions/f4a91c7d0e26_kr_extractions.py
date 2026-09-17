"""add kr_extractions table + FilePurpose.KR_PAGE_IMAGE

Revision ID: f4a91c7d0e26
Revises: 86e1699c8226
Create Date: 2026-09-17 00:00:00.000000

Постраничный разбор КР (0066-c): одна запись на клиента, `pages` — JSON-список
{page_number, text, image_file_id}, перезаписывается при повторном запуске.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f4a91c7d0e26'
down_revision: Union[str, None] = '86e1699c8226'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Autogenerate doesn't detect added enum values on an existing pg enum type,
    # so this is added by hand (see f030f9e91731 for the same precedent).
    op.execute("ALTER TYPE file_purpose ADD VALUE IF NOT EXISTS 'KR_PAGE_IMAGE'")

    op.create_table(
        'kr_extractions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('client_id', sa.Integer(), nullable=False),
        sa.Column('pages', sa.JSON(), nullable=False),
        sa.Column('extracted_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['client_id'], ['clients.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('client_id'),
    )


def downgrade() -> None:
    op.drop_table('kr_extractions')
    # Postgres doesn't support removing an enum value; left in place on downgrade.
