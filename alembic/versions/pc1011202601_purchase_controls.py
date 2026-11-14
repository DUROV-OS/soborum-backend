"""Documented supplier decisions and comparable prices."""
import json
from alembic import op
import sqlalchemy as sa
revision='pc1011202601'
down_revision='ei1011202601'
branch_labels=depends_on=None
TABLES=('purchase_control_records','purchase_control_documents')


def upgrade():
    op.create_table(TABLES[0], sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('supplier_id', sa.Integer(), sa.ForeignKey('suppliers.id'), nullable=False),
        sa.Column('order_id', sa.Integer(), sa.ForeignKey('supplier_orders.id')),
        sa.Column('kind', sa.String(24), nullable=False),
        sa.Column('request_id', sa.String(36), unique=True, nullable=False),
        sa.Column('actor_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('content', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    for column in ('supplier_id','order_id','kind'):
        op.create_index('ix_purchase_control_records_'+column, TABLES[0], [column])
    op.create_table(TABLES[1], sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('supplier_id', sa.Integer(), sa.ForeignKey('suppliers.id'), nullable=False),
        sa.Column('file_id', sa.Integer(), sa.ForeignKey('file_assets.id'), unique=True, nullable=False),
        sa.Column('actor_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.create_index('ix_purchase_control_documents_supplier_id', TABLES[1], ['supplier_id'])
    connection=op.get_bind()
    if connection.dialect.name=='postgresql':
        inspector=sa.inspect(connection)
        for table in TABLES:
            relations=[{'column':src,'table':fk['referred_table'],'target_column':dst} for fk in inspector.get_foreign_keys(table)
                       for src,dst in zip(fk['constrained_columns'],fk['referred_columns'])]
            args=', '.join("'"+json.dumps(value).replace("'","''")+"'" for value in (['id'],relations))
            op.execute(f'CREATE TRIGGER integrity_capture_change AFTER INSERT OR UPDATE OR DELETE ON public."{table}" FOR EACH ROW EXECUTE FUNCTION public.integrity_capture({args})')
            op.execute(f'CREATE TRIGGER integrity_no_truncate BEFORE TRUNCATE ON public."{table}" FOR EACH STATEMENT EXECUTE FUNCTION public.integrity_reject_mutation()')
            op.execute(f'CREATE TRIGGER purchase_control_immutable BEFORE UPDATE OR DELETE ON public."{table}" FOR EACH ROW EXECUTE FUNCTION public.integrity_reject_mutation()')


def downgrade():
    raise RuntimeError("Retain purchasing evidence; use a compatible application fallback.")
