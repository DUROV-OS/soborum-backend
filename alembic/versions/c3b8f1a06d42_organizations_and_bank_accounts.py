"""организации, банковские счета и счёт у проводки (0081-a)

Revision ID: c3b8f1a06d42
Revises: f1c6d3b78a25
Create Date: 2026-09-24 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c3b8f1a06d42'
down_revision: Union[str, None] = 'f1c6d3b78a25'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Стартовый набор: два юрлица компании и по счёту у каждого. Не демо-данные —
# без счёта проводку создать нельзя, поэтому набор нужен в любом окружении.
_ORGS = [
    ("ООО «ИД Групп»", "ИД Групп"),
    ("ООО «Технология»", "Технология"),
]


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("short_name", sa.String(length=64), nullable=False),
        sa.Column("inn", sa.String(length=16), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "bank_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("bank_name", sa.String(length=255), nullable=True),
        sa.Column("account_number", sa.String(length=34), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="RUB"),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_bank_accounts_organization_id", "bank_accounts", ["organization_id"])

    op.add_column("money_movements", sa.Column("account_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_money_movements_account_id", "money_movements", "bank_accounts", ["account_id"], ["id"]
    )
    op.create_index("ix_money_movements_account_id", "money_movements", ["account_id"])

    # Заводим организации прямо здесь, а не только в сиде приложения: проводки,
    # существующие до этой миграции, должны получить счёт в той же транзакции,
    # иначе между `upgrade` и стартом приложения они остаются без счёта.
    conn = op.get_bind()
    first_account_id = None
    for name, short_name in _ORGS:
        org_id = conn.execute(
            sa.text(
                "INSERT INTO organizations (name, short_name, is_active) "
                "VALUES (:name, :short_name, true) RETURNING id"
            ),
            {"name": name, "short_name": short_name},
        ).scalar_one()
        account_id = conn.execute(
            sa.text(
                "INSERT INTO bank_accounts (organization_id, name, currency, is_default, is_active) "
                "VALUES (:org_id, 'Основной счёт', 'RUB', true, true) RETURNING id"
            ),
            {"org_id": org_id},
        ).scalar_one()
        if first_account_id is None:
            first_account_id = account_id

    if first_account_id is not None:
        conn.execute(
            sa.text("UPDATE money_movements SET account_id = :account_id WHERE account_id IS NULL"),
            {"account_id": first_account_id},
        )


def downgrade() -> None:
    op.drop_index("ix_money_movements_account_id", table_name="money_movements")
    op.drop_constraint("fk_money_movements_account_id", "money_movements", type_="foreignkey")
    op.drop_column("money_movements", "account_id")
    op.drop_index("ix_bank_accounts_organization_id", table_name="bank_accounts")
    op.drop_table("bank_accounts")
    op.drop_table("organizations")
