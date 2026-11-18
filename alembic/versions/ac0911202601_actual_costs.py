"""Immutable incurred cost evidence, versions and personal recognition decisions."""
import json
from alembic import op
import sqlalchemy as sa

revision='ac0911202601'
down_revision='pe0911202601'
branch_labels=depends_on=None
TABLES=('actual_cost_documents','actual_cost_records','actual_cost_versions','actual_cost_decisions','actual_cost_closures')


def upgrade():
    common=lambda:[sa.Column('id',sa.Integer(),primary_key=True),sa.Column('actor_id',sa.Integer(),sa.ForeignKey('users.id'),nullable=False),sa.Column('created_at',sa.DateTime(timezone=True),server_default=sa.func.now(),nullable=False)]
    op.create_table(TABLES[0],*common(),sa.Column('file_id',sa.Integer(),sa.ForeignKey('file_assets.id'),unique=True,nullable=False))
    op.create_table(TABLES[1],*common(),sa.Column('identity',sa.String(64),unique=True,nullable=False),sa.Column('kind',sa.String(20),nullable=False))
    op.create_table(TABLES[2],*common(),sa.Column('record_id',sa.Integer(),sa.ForeignKey('actual_cost_records.id'),nullable=False),
                    sa.Column('version',sa.Integer(),nullable=False),sa.Column('request_id',sa.String(36),unique=True,nullable=False),
                    sa.Column('content',sa.JSON(),nullable=False),sa.UniqueConstraint('record_id','version',name='uq_actual_cost_version'))
    op.create_index('ix_actual_cost_versions_record_id',TABLES[2],['record_id'])
    op.create_table(TABLES[3],*common(),sa.Column('version_id',sa.Integer(),sa.ForeignKey('actual_cost_versions.id'),nullable=False),
                    sa.Column('action',sa.String(20),nullable=False),sa.Column('request_id',sa.String(36),unique=True,nullable=False),
                    sa.Column('content',sa.JSON(),nullable=False),sa.UniqueConstraint('version_id','action',name='uq_actual_cost_decision'))
    op.create_index('ix_actual_cost_decisions_version_id',TABLES[3],['version_id'])
    op.create_table(TABLES[4],*common(),sa.Column('project_id',sa.Integer(),sa.ForeignKey('costing_estimates.id'),nullable=False),
                    sa.Column('stage',sa.String(20),nullable=False),sa.Column('source_hash',sa.String(64),nullable=False),
                    sa.Column('request_id',sa.String(36),unique=True,nullable=False),sa.Column('content',sa.JSON(),nullable=False))
    op.create_index('ix_actual_cost_closures_project_id',TABLES[4],['project_id'])
    connection=op.get_bind()
    if connection.dialect.name=='postgresql':
        inspector=sa.inspect(connection)
        for table in TABLES:
            relations=[{'column':src,'table':fk['referred_table'],'target_column':dst} for fk in inspector.get_foreign_keys(table)
                       for src,dst in zip(fk['constrained_columns'],fk['referred_columns'])]
            args=', '.join("'"+json.dumps(value).replace("'","''")+"'" for value in (['id'],relations))
            op.execute(f'CREATE TRIGGER integrity_capture_change AFTER INSERT OR UPDATE OR DELETE ON public."{table}" FOR EACH ROW EXECUTE FUNCTION public.integrity_capture({args})')
            op.execute(f'CREATE TRIGGER integrity_no_truncate BEFORE TRUNCATE ON public."{table}" FOR EACH STATEMENT EXECUTE FUNCTION public.integrity_reject_mutation()')
            op.execute(f'CREATE TRIGGER actual_cost_immutable BEFORE UPDATE OR DELETE ON public."{table}" FOR EACH ROW EXECUTE FUNCTION public.integrity_reject_mutation()')


def downgrade():
    raise RuntimeError('Retain incurred cost history; use a compatible application fallback.')
