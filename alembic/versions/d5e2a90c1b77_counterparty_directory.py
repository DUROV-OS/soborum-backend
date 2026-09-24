"""единый справочник контрагентов + контрагент у проводки (0081-c)

Revision ID: d5e2a90c1b77
Revises: c3b8f1a06d42
Create Date: 2026-09-24 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'd5e2a90c1b77'
# Ветки задачи 0081 уходят в main одной приёмкой, поэтому миграции выстроены в
# цепочку (0081-a -> 0081-c), а не веером от общего предка: две головы alembic
# — это неподнявшийся бэкенд в проде.
down_revision: Union[str, None] = 'c3b8f1a06d42'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Тип создаём явно, а в create_table передаём его с create_type=False —
    # иначе SQLAlchemy выпустит CREATE TYPE второй раз и миграция упадёт на
    # «type counterparty_kind already exists» (тот же приём, что в
    # 1b47820961ec).
    sa.Enum(
        "CLIENT", "SUPPLIER", "EMPLOYEE", "GOVERNMENT", "OTHER", name="counterparty_kind"
    ).create(op.get_bind(), checkfirst=True)
    counterparty_kind = postgresql.ENUM(
        "CLIENT",
        "SUPPLIER",
        "EMPLOYEE",
        "GOVERNMENT",
        "OTHER",
        name="counterparty_kind",
        create_type=False,
    )

    op.create_table(
        "counterparties",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("inn", sa.String(length=16), nullable=True),
        sa.Column("kind", counterparty_kind, nullable=False, server_default="OTHER"),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id"), nullable=True),
        sa.Column("supplier_id", sa.Integer(), sa.ForeignKey("suppliers.id"), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    # ИНН уникален среди непустых: NULL в UNIQUE Postgres не сравнивает, так
    # что контрагентов без ИНН может быть сколько угодно.
    op.create_unique_constraint("uq_counterparties_inn", "counterparties", ["inn"])
    op.create_index("ix_counterparties_name", "counterparties", ["name"])

    op.add_column("money_movements", sa.Column("counterparty_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_money_movements_counterparty_id",
        "money_movements",
        "counterparties",
        ["counterparty_id"],
        ["id"],
    )
    op.create_index("ix_money_movements_counterparty_id", "money_movements", ["counterparty_id"])


def downgrade() -> None:
    op.drop_index("ix_money_movements_counterparty_id", table_name="money_movements")
    op.drop_constraint("fk_money_movements_counterparty_id", "money_movements", type_="foreignkey")
    op.drop_column("money_movements", "counterparty_id")
    op.drop_index("ix_counterparties_name", table_name="counterparties")
    op.drop_constraint("uq_counterparties_inn", "counterparties", type_="unique")
    op.drop_table("counterparties")
    sa.Enum(name="counterparty_kind").drop(op.get_bind(), checkfirst=True)
