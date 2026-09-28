"""база партнёров и заметки по партнёрам (0083-a)

Revision ID: a3f7c2e91b04
Revises: d5e2a90c1b77
Create Date: 2026-09-28 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'a3f7c2e91b04'
down_revision: Union[str, None] = 'd5e2a90c1b77'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Тип создаём явно и передаём в create_table с create_type=False — иначе
    # SQLAlchemy выпустит CREATE TYPE второй раз (тот же приём, что в d5e2a90c1b77).
    sa.Enum(
        "COMMERCE", "REAL_ESTATE_AGENCY", "REALTOR", "LAND_SPECIALIST", name="partner_category"
    ).create(op.get_bind(), checkfirst=True)
    partner_category = postgresql.ENUM(
        "COMMERCE",
        "REAL_ESTATE_AGENCY",
        "REALTOR",
        "LAND_SPECIALIST",
        name="partner_category",
        create_type=False,
    )

    op.create_table(
        "partners",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("category", partner_category, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("city", sa.String(length=255), nullable=False),
        sa.Column("organization", sa.String(length=255), nullable=True),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("contacts", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_partners_name", "partners", ["name"])
    op.create_index("ix_partners_city", "partners", ["city"])

    op.create_table(
        "partner_notes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "partner_id",
            sa.Integer(),
            sa.ForeignKey("partners.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("author_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_partner_notes_partner_id", "partner_notes", ["partner_id"])


def downgrade() -> None:
    op.drop_index("ix_partner_notes_partner_id", table_name="partner_notes")
    op.drop_table("partner_notes")
    op.drop_index("ix_partners_city", table_name="partners")
    op.drop_index("ix_partners_name", table_name="partners")
    op.drop_table("partners")
    sa.Enum(name="partner_category").drop(op.get_bind(), checkfirst=True)
