"""client chat state and one-chat-one-client uniqueness

Revision ID: b9a2e6775692
Revises: ae8f247e68b8
Create Date: 2026-09-15 01:03:14.555702

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b9a2e6775692'
down_revision: Union[str, None] = 'ae8f247e68b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Состояние переписки — хранится на связи «клиент↔чат MAX», сбрасывается
    # при отвязке (см. app.clients.service.set_max_chat_id).
    chat_state = sa.Enum('AGREEMENT', 'WAITING', 'ANALYSIS', name='client_chat_state')
    chat_state.create(op.get_bind(), checkfirst=True)
    op.add_column('clients', sa.Column('max_chat_state', chat_state, nullable=True))

    # Один чат MAX — не более одного клиента (по аналогии с
    # uq_suppliers_max_chat_id). NULL допускает сколько угодно клиентов без
    # привязанного чата — уникальность действует только на непустые значения.
    op.create_unique_constraint('uq_clients_max_chat_id', 'clients', ['max_chat_id'])


def downgrade() -> None:
    op.drop_constraint('uq_clients_max_chat_id', 'clients', type_='unique')
    op.drop_column('clients', 'max_chat_state')
    sa.Enum(name='client_chat_state').drop(op.get_bind(), checkfirst=True)
