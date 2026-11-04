"""Stock holds and audited partial release/fulfilment; no physical replay."""

from alembic import op
import sqlalchemy as sa

revision = "c184e72a5609"
down_revision = "a672d319e085"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "stock_reservations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "material_id",
            sa.Integer(),
            sa.ForeignKey("warehouse_materials.id"),
            nullable=False,
        ),
        sa.Column("production_id", sa.Integer(), sa.ForeignKey("productions.id")),
        sa.Column(
            "source_order_id", sa.Integer(), sa.ForeignKey("moysklad_records.id")
        ),
        sa.Column("source_line", sa.Integer()),
        sa.Column("purpose", sa.String(255), nullable=False),
        sa.Column("unit", sa.String(32), nullable=False),
        sa.Column("quantity", sa.Numeric(18, 6), nullable=False),
        sa.Column("released_quantity", sa.Numeric(18, 6), nullable=False),
        sa.Column("consumed_quantity", sa.Numeric(18, 6), nullable=False),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("request_id", sa.String(36), unique=True, nullable=False),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("archived", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "quantity > 0 AND released_quantity >= 0 AND consumed_quantity >= 0 AND released_quantity + consumed_quantity <= quantity",
            name="ck_stock_reservations_quantities",
        ),
        sa.CheckConstraint(
            "production_id IS NULL OR source_order_id IS NULL",
            name="ck_stock_reservations_target",
        ),
        sa.CheckConstraint(
            "(source_order_id IS NULL AND source_line IS NULL) OR (source_order_id IS NOT NULL AND source_line IS NOT NULL)",
            name="ck_stock_reservations_order_line",
        ),
        sa.CheckConstraint(
            "NOT archived OR quantity = released_quantity + consumed_quantity",
            name="ck_stock_reservations_archive",
        ),
    )
    for col in ("material_id", "production_id", "source_order_id"):
        op.create_index("ix_stock_reservations_" + col, "stock_reservations", [col])
    op.create_table(
        "stock_reservation_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "reservation_id",
            sa.Integer(),
            sa.ForeignKey("stock_reservations.id"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("quantity", sa.Numeric(18, 6), nullable=False),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("request_id", sa.String(36), unique=True),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("movement_id", sa.Integer(), sa.ForeignKey("stock_movements.id")),
        sa.Column("note", sa.String(500), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_stock_reservation_events_reservation_id",
        "stock_reservation_events",
        ["reservation_id"],
    )


def downgrade():
    op.drop_table("stock_reservation_events")
    op.drop_table("stock_reservations")
