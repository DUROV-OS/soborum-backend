"""client_chat_links: несколько чатов MAX на клиента вместо одного (0053)

Переносит `clients.max_chat_id`/`clients.max_chat_state` в отдельную таблицу
`client_chat_links` (клиент -> чаты, 1:N). Чат по-прежнему принадлежит не более
чем одному клиенту — уникальность `max_chat_id` переезжает на новую таблицу.

Revision ID: a9267e2c5497
Revises: 4392ad07a8eb
Create Date: 2026-09-18 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'a9267e2c5497'
down_revision: Union[str, None] = '4392ad07a8eb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == 'postgresql'

    # Тип client_chat_state уже существует (b9a2e6775692) — переиспользуем его,
    # не создаём заново.
    state_col: sa.types.TypeEngine = (
        postgresql.ENUM('AGREEMENT', 'WAITING', 'ANALYSIS', name='client_chat_state', create_type=False)
        if is_pg
        else sa.Enum('AGREEMENT', 'WAITING', 'ANALYSIS', name='client_chat_state')
    )

    op.create_table(
        'client_chat_links',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('client_id', sa.Integer(), nullable=False),
        sa.Column('max_chat_id', sa.BigInteger(), nullable=False),
        sa.Column('label', sa.String(length=255), nullable=False),
        sa.Column('state', state_col, nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.ForeignKeyConstraint(['client_id'], ['clients.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('max_chat_id', name='uq_client_chat_links_max_chat_id'),
    )
    op.create_index('ix_client_chat_links_client_id', 'client_chat_links', ['client_id'])

    # Перенести существующие привязки: один чат на клиента -> одна строка,
    # лейбл по умолчанию «Клиент» (спека 0053, п.2).
    op.execute(
        """
        INSERT INTO client_chat_links (client_id, max_chat_id, label, state, created_at)
        SELECT id, max_chat_id, 'Клиент', max_chat_state, now()
        FROM clients
        WHERE max_chat_id IS NOT NULL
        """
    )

    op.drop_constraint('uq_clients_max_chat_id', 'clients', type_='unique')
    op.drop_column('clients', 'max_chat_id')
    op.drop_column('clients', 'max_chat_state')


def downgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == 'postgresql'
    state_col: sa.types.TypeEngine = (
        postgresql.ENUM('AGREEMENT', 'WAITING', 'ANALYSIS', name='client_chat_state', create_type=False)
        if is_pg
        else sa.Enum('AGREEMENT', 'WAITING', 'ANALYSIS', name='client_chat_state')
    )
    op.add_column('clients', sa.Column('max_chat_state', state_col, nullable=True))
    op.add_column('clients', sa.Column('max_chat_id', sa.BigInteger(), nullable=True))
    op.create_unique_constraint('uq_clients_max_chat_id', 'clients', ['max_chat_id'])

    # Обратный перенос — берём первую (по id) привязку каждого клиента,
    # остальные (если клиент успел завести несколько) теряются: downgrade —
    # аварийный путь на старую 1:1 схему, не сохраняет данные, недоступные ей.
    op.execute(
        """
        UPDATE clients
        SET max_chat_id = linked.max_chat_id, max_chat_state = linked.state
        FROM (
            SELECT DISTINCT ON (client_id) client_id, max_chat_id, state
            FROM client_chat_links
            ORDER BY client_id, id
        ) AS linked
        WHERE clients.id = linked.client_id
        """
    )

    op.drop_index('ix_client_chat_links_client_id', table_name='client_chat_links')
    op.drop_table('client_chat_links')
