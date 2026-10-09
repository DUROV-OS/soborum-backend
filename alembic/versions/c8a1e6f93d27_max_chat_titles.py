"""max_chat_titles: своё название чата MAX в системе (0106)

Revision ID: c8a1e6f93d27
Revises: 205d05997209
Create Date: 2026-10-09 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c8a1e6f93d27'
down_revision: Union[str, None] = '205d05997209'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'max_chat_titles',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('max_chat_id', sa.BigInteger(), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('max_chat_id', name='uq_max_chat_titles_max_chat_id'),
    )


def downgrade() -> None:
    op.drop_table('max_chat_titles')
