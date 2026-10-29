"""Native customer orders, shipments and their reservation targets."""

from alembic import op
import sqlalchemy as sa

revision = "d287f63b910a"
down_revision = "c184e72a5609"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "sales_orders",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_id", sa.String(255), unique=True),
        sa.Column(
            "source_record_id",
            sa.Integer(),
            sa.ForeignKey("moysklad_records.id"),
            unique=True,
        ),
        sa.Column("request_id", sa.String(36), unique=True),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("number", sa.String(255), nullable=False),
        sa.Column(
            "counterparty_id",
            sa.Integer(),
            sa.ForeignKey("counterparties.id"),
            nullable=False,
        ),
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("source_state", sa.String(255)),
        sa.Column("archived", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("total", sa.Numeric(18, 2), nullable=False),
        sa.Column("opening_paid", sa.Numeric(18, 2), nullable=False),
        sa.Column("lines", sa.JSON(), nullable=False),
        sa.Column("expected_at", sa.Date()),
        sa.Column("address", sa.String(1000), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_sales_orders_counterparty_id", "sales_orders", ["counterparty_id"]
    )
    op.create_table(
        "sales_shipments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_id", sa.String(255), unique=True),
        sa.Column("request_id", sa.String(36), unique=True),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("sales_orders.id")),
        sa.Column(
            "counterparty_id",
            sa.Integer(),
            sa.ForeignKey("counterparties.id"),
            nullable=False,
        ),
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("number", sa.String(255), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("historical", sa.Boolean(), nullable=False),
        sa.Column("archived", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("total", sa.Numeric(18, 2), nullable=False),
        sa.Column("lines", sa.JSON(), nullable=False),
        sa.Column("received_by", sa.String(255), nullable=False),
        sa.Column("address", sa.String(1000), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "operation_id", sa.Integer(), sa.ForeignKey("warehouse_operations.id")
        ),
        sa.Column("post_request_id", sa.String(36), unique=True),
        sa.Column("post_parameters", sa.JSON()),
        sa.Column("posted_by_id", sa.Integer(), sa.ForeignKey("users.id")),
        sa.Column("posted_at", sa.DateTime(timezone=True)),
        sa.Column("cancel_request_id", sa.String(36), unique=True),
        sa.Column("cancel_parameters", sa.JSON()),
        sa.Column(
            "cancel_operation_id",
            sa.Integer(),
            sa.ForeignKey("warehouse_operations.id"),
        ),
        sa.Column("cancelled_by_id", sa.Integer(), sa.ForeignKey("users.id")),
        sa.Column("cancelled_at", sa.DateTime(timezone=True)),
        sa.Column("cancellation_note", sa.String(500)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_sales_shipments_order_id", "sales_shipments", ["order_id"])
    op.create_table(
        "sales_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("sales_orders.id")),
        sa.Column("shipment_id", sa.Integer(), sa.ForeignKey("sales_shipments.id")),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("request_id", sa.String(36), unique=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_sales_events_order_id", "sales_events", ["order_id"])
    op.create_index("ix_sales_events_shipment_id", "sales_events", ["shipment_id"])
    op.add_column("stock_reservations", sa.Column("sales_order_id", sa.Integer()))
    op.add_column("stock_reservations", sa.Column("sales_line", sa.Integer()))
    op.create_foreign_key(
        "fk_stock_reservations_sales_order",
        "stock_reservations",
        "sales_orders",
        ["sales_order_id"],
        ["id"],
    )
    op.create_index(
        "ix_stock_reservations_sales_order_id", "stock_reservations", ["sales_order_id"]
    )
    op.drop_constraint(
        "ck_stock_reservations_target", "stock_reservations", type_="check"
    )
    op.create_check_constraint(
        "ck_stock_reservations_target",
        "stock_reservations",
        "(production_id IS NULL OR (source_order_id IS NULL AND sales_order_id IS NULL)) AND (source_order_id IS NULL OR sales_order_id IS NULL)",
    )
    op.create_check_constraint(
        "ck_stock_reservations_sales_line",
        "stock_reservations",
        "(sales_order_id IS NULL AND sales_line IS NULL) OR (sales_order_id IS NOT NULL AND sales_line IS NOT NULL)",
    )


def downgrade():
    op.drop_constraint(
        "ck_stock_reservations_sales_line", "stock_reservations", type_="check"
    )
    op.drop_constraint(
        "ck_stock_reservations_target", "stock_reservations", type_="check"
    )
    op.create_check_constraint(
        "ck_stock_reservations_target",
        "stock_reservations",
        "production_id IS NULL OR source_order_id IS NULL",
    )
    op.drop_index(
        "ix_stock_reservations_sales_order_id", table_name="stock_reservations"
    )
    op.drop_constraint(
        "fk_stock_reservations_sales_order", "stock_reservations", type_="foreignkey"
    )
    op.drop_column("stock_reservations", "sales_line")
    op.drop_column("stock_reservations", "sales_order_id")
    op.drop_table("sales_events")
    op.drop_table("sales_shipments")
    op.drop_table("sales_orders")
