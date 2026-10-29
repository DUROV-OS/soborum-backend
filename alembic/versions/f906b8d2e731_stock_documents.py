"""Count and transfer documents with stock journal reasons."""

from alembic import op
import sqlalchemy as sa

revision = "f906b8d2e731"
down_revision = "e80a76f4c213"
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            for enum in ("stock_movement_reason", "warehouse_operation_kind"):
                for value in (
                    "TRANSFER_IN",
                    "TRANSFER_OUT",
                    "COUNT_GAIN",
                    "COUNT_LOSS",
                ):
                    op.execute(f"ALTER TYPE {enum} ADD VALUE IF NOT EXISTS '{value}'")
    op.create_table(
        "stock_documents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("number", sa.String(255), nullable=False),
        sa.Column("warehouse", sa.String(255), nullable=False),
        sa.Column("target_warehouse", sa.String(255)),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("note", sa.String(1000), nullable=False),
        sa.Column("lines", sa.JSON(), nullable=False),
        sa.Column("operation_ids", sa.JSON(), nullable=False),
        sa.Column("source_id", sa.String(255)),
        sa.Column("source_state", sa.String(255)),
        sa.Column("archived", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.String(36), unique=True),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("post_request_id", sa.String(36), unique=True),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("posted_by_id", sa.Integer(), sa.ForeignKey("users.id")),
        sa.Column("posted_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("kind", "source_id", name="uq_stock_documents_source"),
    )
    op.create_index("ix_stock_documents_kind", "stock_documents", ["kind"])


def downgrade():
    op.drop_table("stock_documents")
    # PostgreSQL enum values remain so existing warehouse journals stay readable.
