"""MoySklad source replica, stable native links and archived warehouse records."""

from alembic import op
import sqlalchemy as sa

revision = "e29a7310c284"
down_revision = "d8e3b9a52c17"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "moysklad_records",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("entity_type", sa.String(100), nullable=False),
        sa.Column("source_id", sa.String(255), nullable=False),
        sa.Column("title", sa.String(1000), nullable=False),
        sa.Column("archived", sa.Boolean(), nullable=False),
        sa.Column("state", sa.String(255)),
        sa.Column("search_text", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("entity_type", "source_id", name="uq_moysklad_source"),
    )
    op.create_index("ix_moysklad_records_entity_type", "moysklad_records", ["entity_type"])
    op.create_index("ix_moysklad_records_archived", "moysklad_records", ["archived"])
    op.create_table(
        "moysklad_links",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_key", sa.String(500), nullable=False),
        sa.Column("target_type", sa.String(100), nullable=False),
        sa.Column("target_id", sa.Integer(), nullable=False),
        sa.UniqueConstraint("source_key", "target_type", name="uq_moysklad_target"),
    )
    op.create_table(
        "moysklad_imports",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("imported_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=False),
    )
    op.add_column(
        "warehouse_materials",
        sa.Column("is_archived", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade():
    op.drop_column("warehouse_materials", "is_archived")
    op.drop_table("moysklad_imports")
    op.drop_table("moysklad_links")
    op.drop_table("moysklad_records")
