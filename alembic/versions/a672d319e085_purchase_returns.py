"""Supplier returns and their cash-refund links; existing balances stay unchanged."""

from alembic import op
import sqlalchemy as sa

revision = "a672d319e085"
down_revision = "f906b8d2e731"
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            for name in ("stock_movement_reason", "warehouse_operation_kind"):
                op.execute(
                    f"ALTER TYPE {name} ADD VALUE IF NOT EXISTS 'PURCHASE_RETURN'"
                )
    op.create_table(
        "purchase_returns",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("request_id", sa.String(36), nullable=False, unique=True),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column(
            "receipt_id",
            sa.Integer(),
            sa.ForeignKey("purchase_receipts.id"),
            nullable=False,
        ),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("supplier_orders.id")),
        sa.Column(
            "supplier_id", sa.Integer(), sa.ForeignKey("suppliers.id"), nullable=False
        ),
        sa.Column(
            "operation_id", sa.Integer(), sa.ForeignKey("warehouse_operations.id")
        ),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("number", sa.String(255), nullable=False),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("archived", sa.Boolean(), nullable=False),
        sa.Column("cancelled", sa.Boolean(), nullable=False),
        sa.Column("cancellation_note", sa.String(500)),
        sa.Column("cancelled_by_id", sa.Integer(), sa.ForeignKey("users.id")),
        sa.Column("cancelled_at", sa.DateTime(timezone=True)),
        sa.Column("cancel_request_id", sa.String(36), unique=True),
        sa.Column(
            "cancel_operation_id",
            sa.Integer(),
            sa.ForeignKey("warehouse_operations.id"),
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lines", sa.JSON(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_purchase_returns_receipt_id", "purchase_returns", ["receipt_id"]
    )
    op.create_index("ix_purchase_returns_order_id", "purchase_returns", ["order_id"])
    op.add_column(
        "money_movements",
        sa.Column(
            "purchase_return_id", sa.Integer(), sa.ForeignKey("purchase_returns.id")
        ),
    )
    op.create_index(
        "ix_money_movements_purchase_return_id",
        "money_movements",
        ["purchase_return_id"],
    )


def downgrade():
    op.drop_column("money_movements", "purchase_return_id")
    op.drop_table("purchase_returns")
