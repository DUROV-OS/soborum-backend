"""Native purchasing: source orders, partial receiving and opening payments."""

from alembic import op
import sqlalchemy as sa

revision = "c62e174b9083"
down_revision = "b3e91d6042a8"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("money_movements", sa.Column("request_id", sa.String(36), nullable=True))
    op.create_unique_constraint("uq_money_movements_request_id", "money_movements", ["request_id"])
    op.add_column("supplier_orders", sa.Column("source_id", sa.String(255), nullable=True))
    op.create_unique_constraint("uq_supplier_orders_source_id", "supplier_orders", ["source_id"])
    op.add_column("supplier_orders", sa.Column("number", sa.String(255), nullable=True))
    op.add_column("supplier_orders", sa.Column("source_state", sa.String(255), nullable=True))
    op.add_column(
        "supplier_orders", sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    op.add_column("supplier_orders", sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
    op.add_column(
        "supplier_orders", sa.Column("opening_paid", sa.Numeric(18, 2), nullable=False, server_default="0")
    )
    op.create_table(
        "purchase_receipts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_id", sa.String(255), unique=True),
        sa.Column("request_id", sa.String(36), unique=True),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("supplier_orders.id")),
        sa.Column("supplier_id", sa.Integer(), sa.ForeignKey("suppliers.id")),
        sa.Column("operation_id", sa.Integer(), sa.ForeignKey("warehouse_operations.id")),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("number", sa.String(255), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lines", sa.JSON(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_purchase_receipts_order_id", "purchase_receipts", ["order_id"])


def downgrade():
    op.drop_table("purchase_receipts")
    for name in ["opening_paid", "version", "archived", "source_state", "number"]:
        op.drop_column("supplier_orders", name)
    op.drop_constraint("uq_supplier_orders_source_id", "supplier_orders", type_="unique")
    op.drop_column("supplier_orders", "source_id")
    op.drop_constraint("uq_money_movements_request_id", "money_movements", type_="unique")
    op.drop_column("money_movements", "request_id")
