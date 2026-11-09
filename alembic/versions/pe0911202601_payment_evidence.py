"""Immutable payment evidence and personal accounting review; no historical attestations."""
import json
from alembic import op
import sqlalchemy as sa

revision='pe0911202601'
down_revision='ca0911202601'
branch_labels=depends_on=None
TABLES=('payment_evidence_documents','payment_evidence_versions','payment_evidence_checks')


def upgrade():
    common=lambda:[sa.Column('id',sa.Integer(),primary_key=True),sa.Column('actor_id',sa.Integer(),sa.ForeignKey('users.id'),nullable=False),sa.Column('created_at',sa.DateTime(timezone=True),server_default=sa.func.now(),nullable=False)]
    op.create_table(TABLES[0],*common(),sa.Column('movement_id',sa.Integer(),sa.ForeignKey('money_movements.id'),nullable=False),
                    sa.Column('file_id',sa.Integer(),sa.ForeignKey('file_assets.id'),nullable=False,unique=True))
    op.create_table(TABLES[1],*common(),sa.Column('movement_id',sa.Integer(),sa.ForeignKey('money_movements.id'),nullable=False),
                    sa.Column('version',sa.Integer(),nullable=False),sa.Column('request_id',sa.String(36),unique=True,nullable=False),
                    sa.Column('source_hash',sa.String(64),nullable=False),sa.Column('content',sa.JSON(),nullable=False),
                    sa.UniqueConstraint('movement_id','version',name='uq_payment_evidence_version'))
    op.create_table(TABLES[2],*common(),sa.Column('version_id',sa.Integer(),sa.ForeignKey('payment_evidence_versions.id'),nullable=False,unique=True),
                    sa.Column('request_id',sa.String(36),nullable=False,unique=True),sa.Column('content',sa.JSON(),nullable=False))
    for table in TABLES[:2]: op.create_index('ix_'+table+'_movement_id',table,['movement_id'])
    connection=op.get_bind()
    if connection.dialect.name=='postgresql':
        inspector=sa.inspect(connection)
        for table in TABLES:
            relations=[{'column':src,'table':fk['referred_table'],'target_column':dst} for fk in inspector.get_foreign_keys(table)
                       for src,dst in zip(fk['constrained_columns'],fk['referred_columns'])]
            args=', '.join("'"+json.dumps(value).replace("'","''")+"'" for value in (['id'],relations))
            op.execute(f'CREATE TRIGGER integrity_capture_change AFTER INSERT OR UPDATE OR DELETE ON public."{table}" FOR EACH ROW EXECUTE FUNCTION public.integrity_capture({args})')
            op.execute(f'CREATE TRIGGER integrity_no_truncate BEFORE TRUNCATE ON public."{table}" FOR EACH STATEMENT EXECUTE FUNCTION public.integrity_reject_mutation()')
            op.execute(f'CREATE TRIGGER payment_evidence_immutable BEFORE UPDATE OR DELETE ON public."{table}" FOR EACH ROW EXECUTE FUNCTION public.integrity_reject_mutation()')


def downgrade():
    raise RuntimeError('Retain documentary payment reviews; use a compatible application fallback.')
