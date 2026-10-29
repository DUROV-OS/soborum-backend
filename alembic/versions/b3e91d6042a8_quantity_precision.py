"""Preserve fractional MoySklad recipe quantities throughout stock movements."""

from alembic import op
import sqlalchemy as sa

revision = "b3e91d6042a8"
down_revision = "a290c54b8731"
branch_labels = None
depends_on = None
COLUMNS = {
    "warehouse_materials": ["quantity_in_stock", "threshold", "pack_quantity"],
    "supply_lines": ["quantity"],
    "stock_movements": ["delta", "balance_after"],
    "block_materials": ["quantity_required", "quantity_requested", "quantity_provided"],
    "material_requests": ["quantity"],
    "template_block_materials": ["quantity"],
}


def upgrade():
    for table, columns in COLUMNS.items():
        for column in columns:
            op.alter_column(table, column, existing_type=sa.Numeric(14, 3), type_=sa.Numeric(18, 6))
    op.alter_column(
        "warehouse_materials", "purchase_price", existing_type=sa.Numeric(14, 2), type_=sa.Numeric(20, 8)
    )
    op.add_column("warehouse_materials", sa.Column("catalog_path", sa.String(1000), nullable=True))


def downgrade():
    # Rounding migrated stock is a data loss operation. Restore the owner's
    # pre-migration backup for an actual rollback instead of rounding silently.
    raise RuntimeError("Precision downgrade would round stock; restore a verified backup instead")
