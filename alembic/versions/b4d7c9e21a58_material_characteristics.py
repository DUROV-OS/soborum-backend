"""material characteristics: kind/size/diameter/serial/pack/supplier (0078)

Revision ID: b4d7c9e21a58
Revises: e5a9c247b108
Create Date: 2026-09-23 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'b4d7c9e21a58'
down_revision: Union[str, None] = '18acd0e89e98'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Всё nullable: у материалов, заведённых до 0078, и у импортированных
    # из прайс-листов характеристик нет.
    op.add_column("warehouse_materials", sa.Column("kind", sa.String(length=120), nullable=True))
    op.add_column("warehouse_materials", sa.Column("size", sa.String(length=120), nullable=True))
    op.add_column("warehouse_materials", sa.Column("diameter", sa.String(length=60), nullable=True))
    op.add_column("warehouse_materials", sa.Column("serial_number", sa.String(length=120), nullable=True))
    op.add_column("warehouse_materials", sa.Column("pack_quantity", sa.Numeric(14, 3), nullable=True))
    op.add_column("warehouse_materials", sa.Column("supplier_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_warehouse_materials_supplier_id",
        "warehouse_materials",
        "suppliers",
        ["supplier_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_warehouse_materials_supplier_id", "warehouse_materials", type_="foreignkey")
    for column in ("supplier_id", "pack_quantity", "serial_number", "diameter", "size", "kind"):
        op.drop_column("warehouse_materials", column)
