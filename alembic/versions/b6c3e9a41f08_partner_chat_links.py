"""partner_chat_links: привязка партнёра к чату MAX (0105)

Отдельная таблица по образцу `client_chat_links` (0053), без `state` —
это понятие «архив/работа» чата нужно только клиентскому циклу.

Revision ID: b6c3e9a41f08
Revises: 205d05997209
Create Date: 2026-10-09 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b6c3e9a41f08'
down_revision: Union[str, None] = '205d05997209'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'partner_chat_links',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('partner_id', sa.Integer(), nullable=False),
        sa.Column('max_chat_id', sa.BigInteger(), nullable=False),
        sa.Column('label', sa.String(length=255), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.ForeignKeyConstraint(['partner_id'], ['partners.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('max_chat_id', name='uq_partner_chat_links_max_chat_id'),
    )
    op.create_index('ix_partner_chat_links_partner_id', 'partner_chat_links', ['partner_id'])


def downgrade() -> None:
    op.drop_index('ix_partner_chat_links_partner_id', table_name='partner_chat_links')
    op.drop_table('partner_chat_links')
