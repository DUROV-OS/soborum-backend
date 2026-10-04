"""кто рекомендовал клиента — ссылка на партнёра (0083-c)

Revision ID: b8e4d1f63a27
Revises: a3f7c2e91b04
Create Date: 2026-09-28 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b8e4d1f63a27'
# Цепочкой за базой партнёров (0083-a): без таблицы partners внешний ключ не
# создать, а веер от общего предка дал бы две головы alembic.
down_revision: Union[str, None] = 'a3f7c2e91b04'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("clients", sa.Column("referrer_partner_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_clients_referrer_partner_id",
        "clients",
        "partners",
        ["referrer_partner_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_clients_referrer_partner_id", "clients", ["referrer_partner_id"])


def downgrade() -> None:
    op.drop_index("ix_clients_referrer_partner_id", table_name="clients")
    op.drop_constraint("fk_clients_referrer_partner_id", "clients", type_="foreignkey")
    op.drop_column("clients", "referrer_partner_id")
