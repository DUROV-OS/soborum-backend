"""Immutable inventory scans and confirmed count snapshots."""
import json
from alembic import op
import sqlalchemy as sa
revision='wr1011202601'
down_revision='wc0911202601'
branch_labels=depends_on=None
TABLES=('warehouse_inventory_acts',)


def upgrade():
    op.create_table(TABLES[0],sa.Column('id',sa.Integer(),primary_key=True),
        sa.Column('document_id',sa.Integer(),sa.ForeignKey('stock_documents.id'),nullable=False),
        sa.Column('stage',sa.String(16),nullable=False),
        sa.Column('request_id',sa.String(36),unique=True,nullable=False),
        sa.Column('actor_id',sa.Integer(),sa.ForeignKey('users.id'),nullable=False),
        sa.Column('file_id',sa.Integer(),sa.ForeignKey('file_assets.id'),nullable=False),
        sa.Column('content',sa.JSON(),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),server_default=sa.func.now(),nullable=False))
    op.create_index('ix_warehouse_inventory_acts_document_id',TABLES[0],['document_id'])
    connection=op.get_bind()
    if connection.dialect.name=='postgresql':
        inspector=sa.inspect(connection)
        for table in TABLES:
            relations=[{'column':src,'table':fk['referred_table'],'target_column':dst} for fk in inspector.get_foreign_keys(table)
                       for src,dst in zip(fk['constrained_columns'],fk['referred_columns'])]
            args=', '.join("'"+json.dumps(value).replace("'","''")+"'" for value in (['id'],relations))
            op.execute(f'CREATE TRIGGER integrity_capture_change AFTER INSERT OR UPDATE OR DELETE ON public."{table}" FOR EACH ROW EXECUTE FUNCTION public.integrity_capture({args})')
            op.execute(f'CREATE TRIGGER integrity_no_truncate BEFORE TRUNCATE ON public."{table}" FOR EACH STATEMENT EXECUTE FUNCTION public.integrity_reject_mutation()')
            op.execute(f'CREATE TRIGGER warehouse_control_immutable BEFORE UPDATE OR DELETE ON public."{table}" FOR EACH ROW EXECUTE FUNCTION public.integrity_reject_mutation()')


def downgrade():
    raise RuntimeError("Retain inventory evidence; use a compatible application fallback.")
