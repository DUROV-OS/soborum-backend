"""client max_chat_id: привязка клиента к чату в мессенджере MAX

Revision ID: f5a2c8e1d7b3
Revises: c9a1e5b73f28
Create Date: 2026-09-08 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f5a2c8e1d7b3'
down_revision: Union[str, None] = 'c9a1e5b73f28'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ID чата MAX (app/max), к которому привязана переписка с клиентом.
    # nullable — привязка необязательна. BigInteger: id групп/каналов MAX
    # бывают отрицательными и большими; 0 — «Избранное».
    op.add_column('clients', sa.Column('max_chat_id', sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column('clients', 'max_chat_id')
