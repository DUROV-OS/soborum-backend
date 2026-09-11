"""supplier_orders + Supplier взаиморасчёты; MoneyMovement.supply_id → supplier_orders

Задача 0011-d. `warehouse.Supply` (приход материалов на склад, таблица
`supplies`) — отдельная, не связанная с этой фичей сущность; заказ у
поставщика заводим отдельно, чтобы не путать с ней и не менять её поведение.

Revision ID: c4a8f2e17d90
Revises: b3e8d1f6a204
Create Date: 2026-09-11 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'c4a8f2e17d90'
down_revision: Union[str, None] = 'b3e8d1f6a204'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == 'postgresql'

    # Как в b7e3f1a92c50: на PostgreSQL создаём enum-тип явно через raw SQL и
    # передаём колонке ENUM(create_type=False), чтобы SQLAlchemy не пытался
    # выпустить CREATE TYPE повторно во время create_table.
    if is_pg:
        op.execute("DROP TYPE IF EXISTS supplier_order_status")
        op.execute("CREATE TYPE supplier_order_status AS ENUM ('ORDERED', 'IN_TRANSIT', 'RECEIVED')")
        status_col: sa.types.TypeEngine = postgresql.ENUM(
            'ORDERED', 'IN_TRANSIT', 'RECEIVED', name='supplier_order_status', create_type=False
        )
    else:
        status_col = sa.Enum('ORDERED', 'IN_TRANSIT', 'RECEIVED', name='supplier_order_status')

    op.create_table(
        'supplier_orders',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('supplier_id', sa.Integer(), nullable=False),
        # позиции заказа: [{"material", "category", "quantity", "unit_price"}]
        sa.Column('items', sa.JSON(), nullable=False),
        sa.Column('total_cost', sa.Numeric(precision=14, scale=2), server_default='0', nullable=False),
        sa.Column('currency', sa.String(length=3), server_default='RUB', nullable=False),
        sa.Column('expected_at', sa.Date(), nullable=True),
        sa.Column('status', status_col, server_default='ORDERED', nullable=False),
        sa.Column('received_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('comment', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['supplier_id'], ['suppliers.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_supplier_orders_supplier_id', 'supplier_orders', ['supplier_id'])
    op.create_index('ix_supplier_orders_status', 'supplier_orders', ['status'])

    # Взаиморасчёты на Supplier.
    op.add_column('suppliers', sa.Column('total_ordered', sa.Numeric(precision=14, scale=2), server_default='0', nullable=False))
    op.add_column('suppliers', sa.Column('total_paid', sa.Numeric(precision=14, scale=2), server_default='0', nullable=False))

    # MoneyMovement.supply_id раньше ссылался на warehouse.Supply (`supplies`);
    # переключаем на новую SupplierOrder. Поле/имя в API не меняются.
    op.drop_constraint('money_movements_supply_id_fkey', 'money_movements', type_='foreignkey')
    op.create_foreign_key(
        'money_movements_supply_id_fkey', 'money_movements', 'supplier_orders', ['supply_id'], ['id']
    )


def downgrade() -> None:
    op.drop_constraint('money_movements_supply_id_fkey', 'money_movements', type_='foreignkey')
    op.create_foreign_key(
        'money_movements_supply_id_fkey', 'money_movements', 'supplies', ['supply_id'], ['id']
    )

    op.drop_column('suppliers', 'total_paid')
    op.drop_column('suppliers', 'total_ordered')

    op.drop_index('ix_supplier_orders_status', table_name='supplier_orders')
    op.drop_index('ix_supplier_orders_supplier_id', table_name='supplier_orders')
    op.drop_table('supplier_orders')
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        op.execute("DROP TYPE IF EXISTS supplier_order_status")
