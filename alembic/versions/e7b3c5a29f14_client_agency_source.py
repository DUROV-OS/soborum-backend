"""client source: via_agency / agency_name / agency_contact (0079-c)

Revision ID: e7b3c5a29f14
Revises: d2f4a8b16c93
Create Date: 2026-09-23 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'e7b3c5a29f14'
# Цепляемся за миграцию стадий из 0079-a, а не за общего предка: обе ветки
# задачи 0079 уходят в main в одну приёмку, а две головы alembic — это
# неподнявшийся бэкенд в проде.
down_revision: Union[str, None] = 'd2f4a8b16c93'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Все клиенты, заведённые до 0079, считаются прямыми: отметки об агентстве
    # до этой задачи просто не существовало, придумывать её задним числом
    # нельзя.
    op.add_column(
        "clients",
        sa.Column("via_agency", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column("clients", sa.Column("agency_name", sa.String(length=255), nullable=True))
    op.add_column("clients", sa.Column("agency_contact", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("clients", "agency_contact")
    op.drop_column("clients", "agency_name")
    op.drop_column("clients", "via_agency")
